import tempfile
from email.utils import formatdate
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from fastmcp import Client

from rss_addon.http import Page
from rss_addon.server import PrivateBearerVerifier, make_server
from rss_addon.store import Store
from test_feeds import GOOGLE


class MCPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "rss.db")
        self.http = AsyncMock()
        body = GOOGLE.replace(b'Wed, 25 Nov 2026 08:00:00 GMT', formatdate(time.time() - 60, usegmt=True).encode())
        self.http.fetch.side_effect = lambda url, **kwargs: Page(url, body)
        self.server = make_server(self.store, http=self.http)

    async def test_tools_and_catalog_checkpoint_flow(self):
        async with Client(self.server) as client:
            tools = {t.name: t for t in await client.list_tools()}
            self.assertEqual({"list_sources", "get_headlines", "search_news", "read_articles",
                              "ack_headlines", "manage_sources", "manage_groups"}, set(tools))
            async def call(tool_name, **args):
                return (await client.call_tool(tool_name, args)).structured_content
            await call("manage_sources", action="upsert", source_id="reuters", kind="google_news",
                       query="site:reuters.com", name="Reuters")
            await call("manage_groups", action="upsert", group_id="breaking", sources=["reuters"])
            catalog = await call("list_sources")
            self.assertNotIn("config", catalog["sources"][0])
            self.assertEqual(["reuters"], catalog["groups"][0]["sources"])
            since = int(time.time()) - 7 * 86400
            first = await call("get_headlines", groups=["breaking"], since=since, checkpoint="breaking")
            self.assertEqual([[1, "Rates & markets"]], first["sources"]["Reuters"])
            self.assertNotIn("url", str(first))
            self.assertEqual(1, (await call("get_headlines", groups=["breaking"], since=since,
                                            checkpoint="breaking"))["count"])
            await call("ack_headlines", batch_id=first["batch_id"])
            self.assertEqual(0, (await call("get_headlines", groups=["breaking"], since=since,
                                            checkpoint="breaking"))["count"])
            self.assertEqual(1, (await call("search_news", query="test", since=since))["count"])
            await call("manage_sources", action="disable", source_id="reuters")
            self.assertFalse((await call("list_sources"))["sources"][0]["enabled"])

    async def test_stateless_continuation_does_not_refresh(self):
        from rss_addon.store import Article
        source = self.store.put_source("reuters", "Reuters", "google_news", {"query": "test"})
        now = int(time.time())
        self.store.ingest(source["stream"], [Article(f"test:{i}", str(i), "Test", f"https://example.com/{i}", now)
                                             for i in range(3)])
        self.http.fetch.side_effect = lambda url, **kwargs: Page(url, b'<rss version="2.0"><channel><title>Empty</title></channel></rss>')
        async with Client(self.server) as client:
            args = {"sources": ["reuters"], "limit": 1}
            first = (await client.call_tool("get_headlines", args)).structured_content
            self.assertTrue(first["has_more"])
            second = (await client.call_tool("get_headlines", {**args, "page": first["next_page"]})).structured_content
            third = (await client.call_tool("get_headlines", {**args, "page": second["next_page"]})).structured_content
            self.assertFalse(third["has_more"])
            self.assertEqual(1, self.http.fetch.await_count)
            self.assertEqual([1, 2, 3], [result["sources"]["Test"][0][0] for result in (first, second, third)])

    async def test_private_token(self):
        path = Path(self.tmp.name) / "token"
        path.write_text("a" * 64)
        path.chmod(0o600)
        verifier = PrivateBearerVerifier(path)
        self.assertIsNotNone(await verifier.verify_token("a" * 64))
        self.assertIsNone(await verifier.verify_token("wrong"))
        path.chmod(0o644)
        with self.assertRaises(ValueError):
            PrivateBearerVerifier(path)
