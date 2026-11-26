"""SQLite catalog, article identity, and explicit checkpoint acknowledgements."""

import json
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Article:
    # The normalization layer supplies a conservative namespaced identity.
    identity: str
    title: str
    publisher: str
    url: str
    published: int | None = None
    description: str | None = None


class Store:
    def __init__(self, path: str | Path, *, retention_days=30):
        if not 1 <= retention_days <= 365:
            raise ValueError("Retention must be 1-365 days")
        self.path = str(path)
        self.retention_seconds = retention_days * 86400
        with self._db(write=True) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sources (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('rss','google_news')),
                    config TEXT NOT NULL, enabled INTEGER NOT NULL,
                    stream TEXT NOT NULL UNIQUE
                );
                CREATE TABLE IF NOT EXISTS groups (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS members (
                    group_id TEXT REFERENCES groups(id) ON DELETE CASCADE,
                    source_id TEXT REFERENCES sources(id) ON DELETE CASCADE,
                    PRIMARY KEY(group_id, source_id)
                );
                CREATE TABLE IF NOT EXISTS articles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    identity TEXT UNIQUE NOT NULL, title TEXT NOT NULL,
                    publisher TEXT NOT NULL, url TEXT NOT NULL,
                    published INTEGER, description TEXT, resolved_url TEXT
                );
                CREATE TABLE IF NOT EXISTS observations (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    stream TEXT NOT NULL, article_id INTEGER NOT NULL REFERENCES articles(id),
                    observed INTEGER NOT NULL, last_seen INTEGER NOT NULL, UNIQUE(stream, article_id)
                );
                CREATE INDEX IF NOT EXISTS observations_stream_seq
                    ON observations(stream, seq);
                CREATE INDEX IF NOT EXISTS observations_article ON observations(article_id);
                CREATE TABLE IF NOT EXISTS checkpoints (
                    name TEXT NOT NULL, stream TEXT NOT NULL,
                    last_seq INTEGER NOT NULL DEFAULT 0,
                    initial_until INTEGER NOT NULL, initial_since INTEGER NOT NULL,
                    expired INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(name, stream)
                );
                CREATE TABLE IF NOT EXISTS batches (
                    id TEXT PRIMARY KEY, checkpoint TEXT NOT NULL,
                    positions TEXT NOT NULL, created INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS maintenance (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    last_prune INTEGER NOT NULL, generation INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO maintenance VALUES (1,0,0);
            """)
            # Upgrade development databases created by earlier versions.
            if "last_seen" not in {r[1] for r in db.execute("PRAGMA table_info(observations)")}:
                db.execute("ALTER TABLE observations ADD COLUMN last_seen INTEGER NOT NULL DEFAULT 0")
                db.execute("UPDATE observations SET last_seen=observed")
            if "expired" not in {r[1] for r in db.execute("PRAGMA table_info(checkpoints)")}:
                db.execute("ALTER TABLE checkpoints ADD COLUMN expired INTEGER NOT NULL DEFAULT 0")
            db.execute("CREATE INDEX IF NOT EXISTS observations_seen ON observations(last_seen)")

    @contextmanager
    def _db(self, *, write=False):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _key(value):
        if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}", value):
            raise ValueError("Invalid catalog or checkpoint ID")
        return value

    @staticmethod
    def _source(row):
        data = dict(row)
        data["config"] = json.loads(data["config"])
        data["enabled"] = bool(data["enabled"])
        return data

    def put_source(self, source_id, name, kind, config, enabled=True):
        """Upsert a complete source definition. Config edits start a new stream."""
        self._key(source_id)
        if not name.strip():
            raise ValueError("Source name required")
        if kind == "rss":
            if set(config) != {"url"} or not isinstance(config["url"], str) or not config["url"].strip():
                raise ValueError("RSS source requires a URL")
            normalized = {"url": config["url"].strip()}
        elif kind == "google_news":
            if set(config) - {"query", "language", "region"}:
                raise ValueError("Unknown Google News configuration field")
            if not isinstance(config.get("query"), str) or not config["query"].strip():
                raise ValueError("Google News source requires a query")
            normalized = {"query": config["query"].strip(),
                          "language": config.get("language", "en").lower(),
                          "region": config.get("region", "US").upper()}
        else:
            raise ValueError("Unsupported source type")
        encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
        with self._db(write=True) as db:
            old = db.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
            stream = (old["stream"] if old and old["kind"] == kind and old["config"] == encoded
                      else uuid.uuid4().hex)
            db.execute("""INSERT INTO sources VALUES (?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name, kind=excluded.kind,
                config=excluded.config, enabled=excluded.enabled, stream=excluded.stream""",
                       (source_id, name, kind, encoded, int(enabled), stream))
            return self._source(db.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone())

    def remove_source(self, source_id):
        with self._db(write=True) as db:
            return bool(db.execute("DELETE FROM sources WHERE id=?", (source_id,)).rowcount)

    def put_group(self, group_id, name, source_ids):
        self._key(group_id)
        if not name.strip():
            raise ValueError("Group name required")
        with self._db(write=True) as db:
            db.execute("""INSERT INTO groups VALUES (?,?)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name""", (group_id, name))
            db.execute("DELETE FROM members WHERE group_id=?", (group_id,))
            for source_id in sorted(set(source_ids)):
                if not db.execute("SELECT 1 FROM sources WHERE id=?", (source_id,)).fetchone():
                    raise ValueError(f"Unknown source: {source_id}")
                db.execute("INSERT INTO members VALUES (?,?)", (group_id, source_id))
        return {"id": group_id, "name": name, "sources": sorted(set(source_ids))}

    def remove_group(self, group_id):
        with self._db(write=True) as db:
            return bool(db.execute("DELETE FROM groups WHERE id=?", (group_id,)).rowcount)

    def catalog(self):
        with self._db() as db:
            sources = [self._source(r) for r in db.execute("SELECT * FROM sources ORDER BY id")]
            groups = [dict(r) for r in db.execute("SELECT * FROM groups ORDER BY id")]
            for group in groups:
                group["sources"] = [r[0] for r in db.execute(
                    "SELECT source_id FROM members WHERE group_id=? ORDER BY source_id", (group["id"],))]
        return {"sources": sources, "groups": groups}

    def select_sources(self, sources=(), groups=()):
        with self._db() as db:
            ids = set(sources)
            for group in groups:
                if not db.execute("SELECT 1 FROM groups WHERE id=?", (group,)).fetchone():
                    raise ValueError(f"Unknown group: {group}")
                ids.update(r[0] for r in db.execute("SELECT source_id FROM members WHERE group_id=?", (group,)))
            selected = []
            for source_id in sorted(ids):
                row = db.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
                if row is None:
                    raise ValueError(f"Unknown source: {source_id}")
                if row["enabled"]:
                    selected.append(self._source(row))
            return selected

    def ingest(self, stream, entries, *, observed=None):
        observed = int(time.time()) if observed is None else observed
        with self._db(write=True) as db:
            for entry in entries:
                db.execute("""INSERT INTO articles(identity,title,publisher,url,published,description)
                    VALUES (?,?,?,?,?,?) ON CONFLICT(identity) DO UPDATE SET
                    title=excluded.title, publisher=excluded.publisher,
                    published=COALESCE(excluded.published,articles.published),
                    description=COALESCE(excluded.description,articles.description)""",
                    (entry.identity, entry.title, entry.publisher, entry.url, entry.published, entry.description))
                article_id = db.execute("SELECT id FROM articles WHERE identity=?", (entry.identity,)).fetchone()[0]
                db.execute("""INSERT INTO observations(stream,article_id,observed,last_seen)
                    VALUES (?,?,?,?) ON CONFLICT(stream,article_id) DO UPDATE SET
                    last_seen=MAX(observations.last_seen,excluded.last_seen)""",
                    (stream, article_id, observed, observed))

    def headlines(self, streams, *, since, checkpoint=None, limit=100,
                  include_description=False, include_timestamp=False, after=0, until=None, generation=None):
        """Return an observation prefix; never acknowledge on read.

        since is an epoch-second bootstrap cutoff (publication, falling back
        to observation time). Later observations ignore that cutoff.
        Call only with streams whose refresh succeeded; the service layer
        reports failed sources separately.
        """
        streams = sorted(set(streams))
        if not 1 <= limit <= 500:
            raise ValueError("Invalid headline limit")
        if checkpoint is not None:
            self._key(checkpoint)
        if checkpoint is not None and (after or until is not None):
            raise ValueError("Checkpoint reads do not use page cursors")
        with self._db(write=checkpoint is not None) as db:
            current_generation = db.execute("SELECT generation FROM maintenance WHERE id=1").fetchone()[0]
            if generation is not None and generation != current_generation:
                raise ValueError("Page expired after history cleanup; restart the read")
            expired_streams = []
            if checkpoint is None and until is None:
                until = db.execute("SELECT COALESCE(MAX(seq),0) FROM observations").fetchone()[0]
            rows = []
            for stream in streams:
                if checkpoint is not None:
                    high = db.execute("SELECT COALESCE(MAX(seq),0) FROM observations WHERE stream=?",
                                      (stream,)).fetchone()[0]
                    db.execute("""INSERT OR IGNORE INTO checkpoints(name,stream,initial_until,initial_since)
                        VALUES (?,?,?,?)""", (checkpoint, stream, high, since))
                    cp = db.execute("SELECT * FROM checkpoints WHERE name=? AND stream=?",
                                    (checkpoint, stream)).fetchone()
                    if cp["expired"]:
                        expired_streams.append(stream)
                        continue
                    condition = "o.seq>? AND (o.seq>? OR COALESCE(a.published,o.observed)>=?)"
                    args = (cp["last_seq"], cp["initial_until"], cp["initial_since"])
                else:
                    condition = "COALESCE(a.published,o.observed)>=? AND o.seq>? AND o.seq<=?"
                    args = (since, after, until)
                # Each stream needs at most limit+1 observations to establish
                # a global prefix; identity is unique within each stream.
                rows.extend(db.execute(f"""SELECT a.*,o.seq,o.stream FROM observations o
                    JOIN articles a ON a.id=o.article_id
                    WHERE o.stream=? AND {condition} ORDER BY o.seq LIMIT ?""",
                    (stream, *args, limit + 1)).fetchall())
            rows.sort(key=lambda r: r["seq"])
            positions = {}
            seen = set()
            publishers = {}
            more = False
            for row in rows:
                article_id = row["id"]
                if article_id not in seen:
                    if len(seen) == limit:
                        more = True
                        break
                    seen.add(article_id)
                    item = [article_id, row["title"]]
                    if include_description:
                        item.append(row["description"])
                    if include_timestamp:
                        item.append(row["published"])
                    publishers.setdefault(row["publisher"], []).append(item)
                positions[row["stream"]] = row["seq"]
            batch_id = None
            if checkpoint is not None and positions:
                batch_id = uuid.uuid4().hex
                db.execute("INSERT INTO batches VALUES (?,?,?,?)",
                           (batch_id, checkpoint, json.dumps(positions), int(time.time())))
            result = {"sources": publishers, "count": len(seen), "has_more": more, "batch_id": batch_id}
            if expired_streams:
                result["expired_streams"] = expired_streams
            if checkpoint is None and more:
                result["generation"] = current_generation
                result["next_after"] = max(positions.values())
                result["until"] = until
            return result

    def acknowledge(self, batch_id):
        """Advance only acknowledged positions. Repeated/stale acks are safe."""
        with self._db(write=True) as db:
            batch = db.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
            if batch is None:
                raise ValueError("Unknown or expired batch")
            positions = json.loads(batch["positions"])
            # Never acknowledge an expired checkpoint, even with an old batch.
            for stream in positions:
                cp = db.execute("SELECT expired FROM checkpoints WHERE name=? AND stream=?",
                                (batch["checkpoint"], stream)).fetchone()
                if cp is None or cp["expired"]:
                    raise ValueError("Checkpoint expired; use a new checkpoint name")
            for stream, seq in positions.items():
                db.execute("""UPDATE checkpoints SET last_seq=MAX(last_seq,?)
                    WHERE name=? AND stream=?""", (seq, batch["checkpoint"], stream))
        return {"acknowledged": True}

    def prune(self, *, now=None, force=False):
        """Prune at most hourly, on demand. Keep entries still present in feeds.

        Explicitly mark checkpoints that lost eligible, unacknowledged rows.
        Do not silently reset their state. A new checkpoint name resynchronizes.
        """
        now = int(time.time()) if now is None else now
        cutoff = now - self.retention_seconds
        with self._db(write=True) as db:
            last = db.execute("SELECT last_prune FROM maintenance WHERE id=1").fetchone()[0]
            if not force and last and now - last < 3600:
                return {"pruned": False}
            db.execute("""UPDATE checkpoints AS c SET expired=1
                WHERE expired=0 AND EXISTS (
                    SELECT 1 FROM observations o JOIN articles a ON a.id=o.article_id
                    WHERE o.stream=c.stream AND o.last_seen<?
                      AND o.seq>c.last_seq
                      AND (o.seq>c.initial_until OR COALESCE(a.published,o.observed)>=c.initial_since)
                )""", (cutoff,))
            removed = db.execute("DELETE FROM observations WHERE last_seen<?", (cutoff,)).rowcount
            articles = db.execute("""DELETE FROM articles WHERE NOT EXISTS
                (SELECT 1 FROM observations WHERE article_id=articles.id)""").rowcount
            batches = db.execute("DELETE FROM batches WHERE created<?", (cutoff,)).rowcount
            # A deleted/reconfigured source never reuses its old stream ID.
            db.execute("""DELETE FROM checkpoints WHERE NOT EXISTS
                (SELECT 1 FROM sources WHERE stream=checkpoints.stream)
                AND NOT EXISTS (SELECT 1 FROM observations WHERE stream=checkpoints.stream)""")
            db.execute("UPDATE maintenance SET last_prune=?, generation=generation+? WHERE id=1",
                       (now, int(removed > 0)))
            return {"pruned": True, "observations": removed, "articles": articles, "batches": batches}

    def article(self, article_id):
        with self._db() as db:
            row = db.execute("SELECT * FROM articles WHERE id=?", (article_id,)).fetchone()
            return dict(row) if row else None

    def remember_url(self, article_id, url):
        with self._db(write=True) as db:
            return bool(db.execute("UPDATE articles SET resolved_url=? WHERE id=?",
                                   (url, article_id)).rowcount)
