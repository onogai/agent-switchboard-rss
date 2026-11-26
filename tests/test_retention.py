import tempfile
import time
import unittest
from pathlib import Path

from rss_addon.store import Article, Store


class RetentionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Store(Path(tmp.name) / "rss.db", retention_days=1)
        self.stream = self.store.put_source("news", "News", "rss", {"url": "https://example.com/feed"})["stream"]
        self.now = int(time.time())
        self.old = self.now - 2 * 86400

    def add(self, key, observed):
        self.store.ingest(self.stream, [Article(key, key, "News", "https://example.com/" + key, observed)],
                          observed=observed)

    def read(self, checkpoint=None, **args):
        return self.store.headlines([self.stream], since=0, checkpoint=checkpoint, **args)

    def test_unacked_expiry_and_acked_checkpoint(self):
        self.add("old", self.old)
        pending = self.read("pending")
        acked = self.read("acked")
        self.store.acknowledge(acked["batch_id"])
        self.assertEqual(1, self.store.prune(now=self.now, force=True)["articles"])
        self.assertEqual([self.stream], self.read("pending")["expired_streams"])
        with self.assertRaises(ValueError):
            self.store.acknowledge(pending["batch_id"])
        self.add("new", self.now)
        self.assertEqual(1, self.read("acked")["count"])
        self.assertEqual(1, self.read("resync")["count"])
        self.assertIsNone(self.store.article(1))

    def test_still_visible_entry_preserves_identity(self):
        self.add("old", self.old)
        first = self.read("news")
        self.store.acknowledge(first["batch_id"])
        self.add("old", self.now)
        self.assertEqual(0, self.store.prune(now=self.now, force=True)["articles"])
        self.assertEqual(0, self.read("news")["count"])
        self.assertEqual(1, self.read()["sources"]["News"][0][0])

    def test_page_is_invalidated_after_prune(self):
        self.add("old", self.old)
        self.add("new", self.now)
        page = self.read(limit=1)
        self.assertTrue(page["has_more"])
        self.store.prune(now=self.now, force=True)
        with self.assertRaisesRegex(ValueError, "Page expired"):
            self.read(after=page["next_after"], until=page["until"], generation=page["generation"])

    def test_prune_rate_limit(self):
        self.assertTrue(self.store.prune(now=self.now)["pruned"])
        self.assertFalse(self.store.prune(now=self.now+60)["pruned"])
