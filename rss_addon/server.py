"""Agent-facing RSS MCP. No dependency on the Switchboard gateway or Task Queue."""

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import stat
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

from fastmcp import FastMCP
from fastmcp.server.auth import AccessToken, TokenVerifier
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field
from starlette.responses import JSONResponse

from .articles import ArticleReader
from .feeds import Collector, google_url, search_source
from .http import HTTPClient, validate_url
from .store import Store


class PrivateBearerVerifier(TokenVerifier):
    def __init__(self, token_file: Path):
        super().__init__()
        info = token_file.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise ValueError("MCP token file must be a private regular file")
        token = token_file.read_text(encoding="utf-8").strip()
        if len(token) < 48 or not token.isascii() or not token.isprintable():
            raise ValueError("Invalid MCP token")
        self.expected = token

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token.isascii() or not hmac.compare_digest(token, self.expected):
            return None
        return AccessToken(token=token, client_id="switchboard-core",
                           subject="switchboard-core", scopes=[])


class Search(BaseModel):
    query: Annotated[str, Field(min_length=1, max_length=2000)]
    language: str = "en"
    region: str = "US"


HeadlineLimit = Annotated[int, Field(ge=1, le=500)]
SourceList = Annotated[list[str], Field(max_length=30)]
SearchList = Annotated[list[Search], Field(max_length=30)]
ArticleIds = Annotated[list[int], Field(max_length=20)]
ArticleURLs = Annotated[list[str], Field(max_length=20)]


def cutoff(since):
    """Relative lookback, bounded to 30 days; absolute UTC epoch seconds allowed."""
    now = int(time.time())
    if isinstance(since, str):
        match = re.fullmatch(r"([1-9]\d{0,4})([mhd])", since)
        if not match:
            raise ValueError("since must be a duration such as 2h or 3d, or UTC epoch seconds")
        since = now - int(match[1]) * {"m": 60, "h": 3600, "d": 86400}[match[2]]
    if type(since) is not int or not now - 30 * 86400 <= since <= now:
        raise ValueError("Lookback must be within the last 30 days")
    return since


def _scope(sources):
    return hashlib.sha256(json.dumps(sorted({s["stream"] for s in sources})).encode()).hexdigest()


def _encode_page(page):
    return base64.urlsafe_b64encode(json.dumps(page, separators=(",", ":")).encode()).decode()


def _decode_page(token, sources):
    try:
        if len(token) > 8192:
            raise ValueError
        data = json.loads(base64.b64decode(token, altchars=b"-_", validate=True))
        if set(data) != {"scope", "streams", "since", "after", "until", "generation"}:
            raise ValueError
        if data["scope"] != _scope(sources):
            raise ValueError
        if not isinstance(data["streams"], list) or not set(data["streams"]) <= {s["stream"] for s in sources}:
            raise ValueError
        if any(type(data[k]) is not int for k in ("since", "after", "until", "generation")):
            raise ValueError
        if not 0 <= data["after"] <= data["until"]:
            raise ValueError
        cutoff(data["since"])
        return data
    except (ValueError, TypeError, KeyError, binascii.Error) as exc:
        raise ValueError("Invalid or expired page cursor; restart the read") from exc


class RSSService:
    def __init__(self, store, http):
        self.store = store
        self.collector = Collector(store, http)
        self.reader = ArticleReader(store, http)

    async def headlines(self, sources, *, since, checkpoint, limit, description, timestamp, page):
        if not sources:
            raise ValueError("Select at least one enabled source or search")
        if checkpoint is not None:
            self.store._key(checkpoint)
        if page and checkpoint is not None:
            raise ValueError("Acknowledge checkpoint batches instead of using page cursors")
        await asyncio.to_thread(self.store.prune)
        if page:
            position = _decode_page(page, sources)
            active = [s for s in sources if s["stream"] in position["streams"]]
            data = await self.collector.headlines(
                active, refresh_sources=False, since=position["since"], after=position["after"],
                until=position["until"], generation=position["generation"], limit=limit, include_description=description,
                include_timestamp=timestamp,
            )
        else:
            position = {"scope": _scope(sources), "since": cutoff(since)}
            data = await self.collector.headlines(
                sources, since=position["since"], checkpoint=checkpoint, limit=limit,
                include_description=description, include_timestamp=timestamp,
            )
        if "next_after" in data:
            position.update(after=data.pop("next_after"), until=data.pop("until"),
                            generation=data.pop("generation"))
            if "page_streams" in data:
                position["streams"] = data.pop("page_streams")
            data["next_page"] = _encode_page(position)
        expired = data.pop("expired_streams", [])
        if expired:
            errors = data.setdefault("errors", {})
            for source in sources:
                if source["stream"] in expired:
                    errors[source["id"]] = "Checkpoint expired; resynchronize with a new checkpoint name"
        if data.get("batch_id") is None:
            data.pop("batch_id", None)
        return data


def make_server(store: Store, *, auth=None, http=None):
    service = None

    @asynccontextmanager
    async def lifespan(server):
        nonlocal service
        if http is not None:
            service = RSSService(store, http)
            yield {}
        else:
            async with HTTPClient() as transport:
                service = RSSService(store, transport)
                yield {}
        service = None

    mcp = FastMCP("Switchboard RSS", auth=auth, lifespan=lifespan)

    @mcp.custom_route("/health", methods=["GET"])
    async def health(request):
        return JSONResponse({"status": "ok"})

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def list_sources(details: bool = False) -> dict:
        """List source IDs, names, status and groups. Set details for feed URLs and query/locale configuration."""
        catalog = await asyncio.to_thread(store.catalog)
        for source in catalog["sources"]:
            source.pop("stream")
            if not details:
                source.pop("config")
        return catalog

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    async def get_headlines(
        sources: SourceList = [], groups: SourceList = [], since: str | int = "24h",
        checkpoint: str | None = None, limit: HeadlineLimit = 100,
        include_description: bool = False, include_timestamp: bool = False,
        page: str | None = None,
    ) -> dict:
        """Fetch named sources/groups. Rows: [article ID, title, optional description, optional UTC epoch time].
        With checkpoint, acknowledge batch_id only after handling; repeat until has_more is false.
        Without checkpoint, use next_page with the same sources/groups. since sets the initial lookback.
        Errors are source-specific; failed sources are not acknowledged. Expired checkpoint: use a new name and lookback. No article bodies or URLs."""
        selected = await asyncio.to_thread(store.select_sources, sources, groups)
        return await service.headlines(selected, since=since, checkpoint=checkpoint, limit=limit,
                                       description=include_description, timestamp=include_timestamp, page=page)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    async def search_news(
        query: Annotated[str | None, Field(max_length=2000)] = None, language: str = "en", region: str = "US",
        searches: SearchList = [], since: str | int = "24h", limit: HeadlineLimit = 100,
        include_description: bool = False, include_timestamp: bool = False, page: str | None = None,
    ) -> dict:
        """Ad hoc Google News search. Supply query or batch searches [{query,language,region}].
        Results use the same compact rows as get_headlines. Follow next_page with the same queries."""
        selected = [search_source(s.query, s.language, s.region) for s in searches]
        if query is not None:
            selected.append(search_source(query, language, region))
        return await service.headlines(selected, since=since, checkpoint=None, limit=limit,
                                       description=include_description, timestamp=include_timestamp, page=page)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    async def read_articles(
        ids: ArticleIds = [], urls: ArticleURLs = [],
        max_chars_per_article: Annotated[int, Field(ge=200, le=30000)] = 8000,
        max_total_chars: Annotated[int, Field(ge=200, le=120000)] = 40000,
    ) -> dict:
        """Read only selected articles by short ID or public URL (up to 20).
        Resolve Google links lazily. Return clean text, publisher URL, errors and truncation."""
        await asyncio.to_thread(store.prune)
        return await service.reader.read(ids, urls, max_chars_per_article=max_chars_per_article,
                                         max_total_chars=max_total_chars)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True))
    async def ack_headlines(batch_id: str) -> dict:
        """Acknowledge a returned batch after successful processing/delivery. Safe to repeat."""
        return await asyncio.to_thread(store.acknowledge, batch_id)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    async def manage_sources(
        action: Literal["upsert", "delete", "enable", "disable"], source_id: str,
        name: str | None = None, kind: Literal["rss", "google_news"] | None = None,
        url: str | None = None, query: str | None = None,
        language: str | None = None, region: str | None = None,
        enabled: bool | None = None,
    ) -> dict:
        """Create/update a named RSS or Google News source, delete it or toggle enabled.
        Upsert preserves omitted fields. New sources require kind and URL/query. Configuration changes start a new stream."""
        def apply():
            if action == "delete":
                return {"deleted": store.remove_source(source_id)}
            old = next((s for s in store.catalog()["sources"] if s["id"] == source_id), None)
            if action in ("enable", "disable"):
                if old is None:
                    raise ValueError("Unknown source")
                return store.put_source(source_id, old["name"], old["kind"], old["config"], action == "enable")
            effective_kind = kind or (old["kind"] if old else None)
            config = dict(old["config"]) if old and old["kind"] == effective_kind else {}
            if effective_kind == "rss":
                if any(x is not None for x in (query, language, region)):
                    raise ValueError("RSS sources use URL, not query or locale")
                if url is not None:
                    config["url"] = url
                validate_url(config.get("url"))
            elif effective_kind == "google_news":
                if url is not None:
                    raise ValueError("Google News sources use query, not URL")
                for key, value in (("query", query), ("language", language), ("region", region)):
                    if value is not None:
                        config[key] = value
                google_url(config.get("query"), config.get("language", "en"), config.get("region", "US"))
            else:
                raise ValueError("Select kind rss or google_news")
            return store.put_source(source_id, name if name is not None else old["name"] if old else source_id,
                                    effective_kind, config, enabled if enabled is not None else old["enabled"] if old else True)

        result = await asyncio.to_thread(apply)
        result.pop("stream", None)
        return result

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    async def manage_groups(
        action: Literal["upsert", "delete", "add_sources", "remove_sources"], group_id: str,
        name: str | None = None, sources: SourceList | None = None,
    ) -> dict:
        """Create/update/delete a flat group or add/remove source memberships.
        Upsert replaces membership only when sources is supplied; a source may belong to multiple groups."""
        def apply():
            if action == "delete":
                return {"deleted": store.remove_group(group_id)}
            old = next((g for g in store.catalog()["groups"] if g["id"] == group_id), None)
            if action != "upsert" and old is None:
                raise ValueError("Unknown group")
            members = set(old["sources"] if old else [])
            if action == "add_sources":
                members.update(sources or [])
            elif action == "remove_sources":
                members.difference_update(sources or [])
            elif sources is not None:
                members = set(sources)
            if len(members) > 30:
                raise ValueError("Group exceeds source limit")
            return store.put_group(group_id, name if name is not None else old["name"] if old else group_id,
                                   members)
        return await asyncio.to_thread(apply)

    return mcp


def main():
    host = os.environ.get("RSS_BIND_HOST", "127.0.0.1")
    token_file = os.environ.get("RSS_MCP_TOKEN_FILE")
    if host != "127.0.0.1" and not token_file:
        raise ValueError("Network-reachable MCP requires RSS_MCP_TOKEN_FILE")
    auth = PrivateBearerVerifier(Path(token_file)) if token_file else None
    store = Store(os.environ.get("RSS_DB", "rss.sqlite3"),
                  retention_days=int(os.environ.get("RSS_RETENTION_DAYS", "30")))
    make_server(store, auth=auth).run(transport="http", host=host,
                                    port=int(os.environ.get("RSS_PORT", "8775")), path="/mcp")


if __name__ == "__main__":
    main()
