import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from rss_addon.articles import ArticleReader, GoogleResolver, extract_article
from rss_addon.http import HTTPError, Page
from rss_addon.store import Article, Store


GOOGLE = "https://news.google.com/rss/articles/test_token"
PUBLISHER = "https://example.com/story"
PARAMS = b'<c-wiz><div jscontroller="x" data-n-a-sg="sig" data-n-a-ts="123"></div></c-wiz>'


def decoded(url, index="0"):
    payload = json.dumps(["garturlres", url])
    return json.dumps([["wrb.fr", "Fbv4je", payload, index]]).encode()


class ArticleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "rss.db")
        self.store.ingest("stream", [Article("google:test_token", "Headline", "Reuters", GOOGLE, 123)])
        self.http = AsyncMock()

    async def test_decode_lazily_and_cache(self):
        async def fetch(url, **kwargs):
            if "batchexecute" in url:
                self.assertEqual("POST", kwargs["method"])
                self.assertEqual("news.google.com", kwargs["allowed_host"])
                return Page(url, decoded(PUBLISHER))
            if "news.google.com" in url:
                return Page(url, PARAMS)
            return Page(url, b"body")
        self.http.fetch.side_effect = fetch
        reader = ArticleReader(self.store, self.http, extractor=lambda page: {"text": "Readable text"})
        result = (await reader.read(ids=[1]))["articles"][0]
        self.assertEqual(PUBLISHER, result["url"])
        self.assertEqual("Readable text", result["text"])
        self.assertEqual(PUBLISHER, self.store.article(1)["resolved_url"])
        self.assertEqual(3, self.http.fetch.await_count)
        self.http.fetch.reset_mock()
        await reader.read(ids=[1])
        self.http.fetch.assert_awaited_once_with(PUBLISHER)

    async def test_reject_private_decoded_url(self):
        self.http.fetch.side_effect = [Page(GOOGLE, PARAMS),
                                       Page(GOOGLE, decoded("http://127.0.0.1/x"))]
        result = (await ArticleReader(self.store, self.http).read(ids=[1]))["articles"][0]
        self.assertIn("error", result)
        self.assertIsNone(self.store.article(1)["resolved_url"])
        self.assertEqual(2, self.http.fetch.await_count)

    async def test_unresolved_never_fetches_as_article(self):
        self.http.fetch.return_value = Page(GOOGLE, b"<html>blocked</html>")
        result = (await ArticleReader(self.store, self.http).read(ids=[1]))["articles"][0]
        self.assertIn("error", result)
        self.assertEqual(1, self.http.fetch.await_count)

    async def test_batch_decode_reordered_results(self):
        second = "https://news.google.com/rss/articles/second"
        rows = [["wrb.fr", "Fbv4je", json.dumps(["garturlres", "https://example.com/b"]), "1"],
                ["wrb.fr", "Fbv4je", json.dumps(["garturlres", PUBLISHER]), "0"]]
        async def fetch(url, **kwargs):
            return Page(url, json.dumps(rows).encode() if "batchexecute" in url else PARAMS)
        self.http.fetch.side_effect = fetch
        self.assertEqual([(PUBLISHER, None), ("https://example.com/b", None)],
                         await GoogleResolver(self.http).resolve([GOOGLE, second]))
        self.assertEqual(3, self.http.fetch.await_count)

    async def test_limits_and_partial_errors(self):
        async def fetch(url, **kwargs):
            if url.endswith("/blocked"):
                raise HTTPError(403)
            return Page(url, b"body")
        self.http.fetch.side_effect = fetch
        reader = ArticleReader(self.store, self.http, extractor=lambda page: {"text": "x" * 500})
        result = (await reader.read(ids=[999], urls=[PUBLISHER, PUBLISHER + "/b",
                                                    PUBLISHER + "/blocked"],
                                    max_chars_per_article=300, max_total_chars=400))["articles"]
        self.assertIn("error", result[0])
        self.assertEqual(300, len(result[1]["text"]))
        self.assertEqual(100, len(result[2]["text"]))
        self.assertTrue(result[1]["truncated"] and result[2]["truncated"])
        self.assertIn("403", result[3]["error"])

    def test_real_extraction_fixture(self):
        sentence = "The research team has published a detailed report describing the results of the experiment. "
        html = ("<html><head><title>Research results</title></head>"
                "<body><nav>Navigation menu</nav><article><h1>Research results</h1>"
                + "".join("<p>" + sentence * 4 + "</p>" for _ in range(4))
                + "</article></body></html>").encode()
        result = extract_article(Page(PUBLISHER, html))
        self.assertIn("research team", result["text"])
        self.assertNotIn("<p>", result["text"])
        self.assertNotIn("Navigation menu", result["text"])
