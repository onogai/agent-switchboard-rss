import tempfile
import unittest
from pathlib import Path

from rss_addon.store import Article, Store


def article(key, published=100, publisher="Reuters"):
    return Article(key, "Headline " + key, publisher, "https://example.com/" + key, published)


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "rss.sqlite3"
        self.store = Store(self.path)
        self.a = self.store.put_source("reuters", "Reuters", "google_news", {"query": "site:reuters.com"})
        self.b = self.store.put_source("bbc", "BBC", "rss", {"url": "https://example.com/rss"})
        self.streams = [self.a["stream"], self.b["stream"]]

    def read(self, checkpoint="digest", **kwargs):
        return self.store.headlines(self.streams, since=kwargs.pop("since", 0),
                                    checkpoint=checkpoint, **kwargs)

    def test_catalog_membership_and_config_revision(self):
        self.store.put_group("world", "World", ["reuters", "bbc"])
        self.store.put_group("breaking", "Breaking", ["reuters"])
        self.assertEqual(2, len(self.store.select_sources(["reuters"], ["world", "breaking"])))
        renamed = self.store.put_source("reuters", "Reuters news", "google_news",
                                       {"query": "site:reuters.com", "region": "us"}, False)
        self.assertEqual(self.a["stream"], renamed["stream"])
        self.assertEqual(["bbc"], [s["id"] for s in self.store.select_sources(groups=["world"])])
        changed = self.store.put_source("reuters", "Reuters", "google_news", {"query": "site:reuters.com oil"})
        self.assertNotEqual(self.a["stream"], changed["stream"])
        with self.assertRaises(ValueError):
            self.store.put_group("world", "Changed", ["unknown"])
        self.assertEqual(["bbc", "reuters"], self.store.catalog()["groups"][1]["sources"])
        self.store.remove_source("bbc")
        self.assertEqual(["reuters"], self.store.catalog()["groups"][1]["sources"])

    def test_compact_dedup_and_stable_id(self):
        for stream in self.streams:
            self.store.ingest(stream, [article("a"), article("a")], observed=100)
        result = self.read()
        self.assertEqual(1, result["count"])
        item = result["sources"]["Reuters"][0]
        self.assertEqual([1, "Headline a"], item)
        self.assertEqual("https://example.com/a", self.store.article(item[0])["url"])
        self.store.remember_url(item[0], "https://publisher.example/a")
        self.store = Store(self.path)
        self.assertEqual("https://publisher.example/a", self.store.article(item[0])["resolved_url"])
        self.assertEqual(item, self.read()["sources"]["Reuters"][0])

    def test_checkpoint_isolation_and_ack(self):
        self.store.ingest(self.streams[0], [article("a"), article("b")])
        first = self.read(limit=1)
        retry = self.read(limit=1)
        self.assertEqual(first["sources"], retry["sources"])
        self.assertTrue(first["has_more"])
        self.store.acknowledge(first["batch_id"])
        second = self.read(limit=1)
        self.assertEqual("Headline b", second["sources"]["Reuters"][0][1])
        self.store.acknowledge(second["batch_id"])
        self.store.acknowledge(retry["batch_id"])
        self.assertEqual(0, self.read()["count"])
        self.assertEqual(2, self.read("breaking")["count"])
        self.assertEqual(2, self.read(None)["count"])
        self.assertEqual(0, self.read()["count"])

    def test_late_observation_and_new_stream_membership(self):
        self.store.ingest(self.streams[0], [article("old", 20), article("recent", 100)], observed=120)
        first = self.read(since=90)
        self.assertEqual(1, first["count"])
        self.store.acknowledge(first["batch_id"])
        self.store.ingest(self.streams[0], [article("late", 10)], observed=130)
        self.store.ingest(self.streams[1], [article("recent", 100)], observed=130)
        later = self.read(since=120)
        self.assertEqual({"Headline late", "Headline recent"},
                         {item[1] for item in later["sources"]["Reuters"]})

    def test_partial_stream_read_does_not_ack_other_stream(self):
        for index, stream in enumerate(self.streams):
            self.store.ingest(stream, [article(str(index))])
        first = self.store.headlines(self.streams[:1], since=0, checkpoint="digest")
        self.store.acknowledge(first["batch_id"])
        self.assertEqual("Headline 1", self.read()["sources"]["Reuters"][0][1])

    def test_unknown_batch_and_invalid_group(self):
        with self.assertRaises(ValueError):
            self.store.acknowledge("missing")
        with self.assertRaises(ValueError):
            self.store.select_sources(groups=["missing"])


if __name__ == "__main__":
    unittest.main()
