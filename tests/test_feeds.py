import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

from aiohttp.resolver import ThreadedResolver

from rss_addon.feeds import (Collector, FeedError, article_identity, google_url,
                             parse_feed, search_source)
from rss_addon.http import FetchError, Page, PublicResolver, UnsafeDestination, validate_url
from rss_addon.store import Store


GOOGLE = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Google News</title>
<item><title>Rates &amp; markets - Reuters</title>
<link>https://news.google.com/rss/articles/token?oc=5</link>
<source url="https://reuters.com">Reuters</source>
<description><![CDATA[<a href="https://example.com">Rates &amp; markets</a> Reuters]]></description>
<pubDate>Wed, 25 Nov 2026 08:00:00 GMT</pubDate></item>
</channel></rss>"""

RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Example News</title>
<item><title>New article</title><link>https://example.com/articles/1</link>
<description><![CDATA[<p>Useful <b>details</b>.</p><script>bad()</script>]]></description>
<pubDate>Wed, 25 Nov 2026 08:00:00 GMT</pubDate></item>
</channel></rss>"""

ATOM = b"""<feed xmlns="http://www.w3.org/2005/Atom">
<title>Science</title><entry><id>urn:example:1</id><title>Study</title>
<link href="https://example.com/study" /><published>2026-11-25T08:00:00Z</published>
<summary type="html">A useful result</summary></entry></feed>"""

EMPTY = b"<rss version='2.0'><channel><title>Empty</title></channel></rss>"


class NormalizationTests(unittest.TestCase):
    def test_google_search_locale_and_query(self):
        params = parse_qs(urlsplit(google_url("site:reuters.com when:2h", "es", "es")).query)
        self.assertEqual(["site:reuters.com when:2h"], params["q"])
        self.assertEqual(["ES:es"], params["ceid"])
        self.assertEqual(search_source("test", "en", "us")["stream"],
                         search_source("test", "en", "US")["stream"])

    def test_google_headline_and_identity(self):
        result = parse_feed(GOOGLE, "https://news.google.com/rss/search", kind="google_news", name="Reuters")
        article = result.articles[0]
        self.assertEqual("Rates & markets", article.title)
        self.assertEqual("Reuters", article.publisher)
        self.assertEqual("google:token", article.identity)
        self.assertEqual(article.identity, article_identity("https://news.google.com/read/token"))
        self.assertIsNone(article.description)
        self.assertIsNotNone(article.published)

    def test_rss_and_atom(self):
        for body, expected in ((RSS, "Useful details."), (ATOM, "A useful result")):
            entry = parse_feed(body, "https://example.com/feed", kind="rss", name="Example").articles[0]
            self.assertEqual(expected, entry.description)
            self.assertIsNotNone(entry.published)

    def test_empty_is_not_invalid(self):
        self.assertEqual([], parse_feed(EMPTY, "https://example.com/feed", kind="rss", name="X").articles)
        with self.assertRaises(FeedError):
            parse_feed(b"<html>Not RSS</html>", "https://example.com/feed", kind="rss", name="X")

    def test_entry_limit(self):
        with self.assertRaises(FeedError):
            parse_feed(RSS, "https://example.com/feed", kind="rss", name="X", max_entries=0)

    def test_public_url_validation(self):
        for url in ("http://127.0.0.1/x", "http://[::1]/", "http://10.0.0.1",
                    "http://169.254.169.254", "http://[::ffff:192.168.0.1]/",
                    "http://localhost.", "http://x.local", "file:///tmp/file",
                    "http://user@example.com", "http://example.com:8080"):
            with self.subTest(url=url), self.assertRaises(UnsafeDestination):
                validate_url(url)
        self.assertEqual("https://example.com/feed", validate_url("https://example.com/feed"))


class CollectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_dns_result_is_checked(self):
        resolver = PublicResolver()
        self.addAsyncCleanup(resolver.close)
        with patch.object(ThreadedResolver, "resolve", new=AsyncMock(
                return_value=[{"host": "8.8.8.8"}, {"host": "127.0.0.1"}])):
            with self.assertRaises(UnsafeDestination):
                await resolver.resolve("example.com")

    async def test_partial_refresh_and_no_article_fetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "rss.db")
            reuters = store.put_source("reuters", "Reuters", "google_news", {"query": "site:reuters.com"})
            broken = store.put_source("broken", "Broken", "rss", {"url": "https://example.com/broken"})
            http = AsyncMock()

            async def fetch(url):
                if "broken" in url:
                    raise FetchError("HTTP 503")
                return Page(url, GOOGLE)

            http.fetch.side_effect = fetch
            collector = Collector(store, http)
            result = await collector.headlines([reuters, broken, reuters], since=0, checkpoint="digest")
            self.assertEqual(1, result["count"])
            self.assertEqual({"broken": "HTTP 503"}, result["errors"])
            self.assertEqual([[1, "Rates & markets"]], result["sources"]["Reuters"])
            self.assertEqual(2, http.fetch.await_count)
            self.assertNotIn("url", str(result))
            store.acknowledge(result["batch_id"])
            again = await collector.headlines([reuters, broken], since=0, checkpoint="digest")
            self.assertEqual(0, again["count"])
            self.assertIn("broken", again["errors"])


if __name__ == "__main__":
    unittest.main()
