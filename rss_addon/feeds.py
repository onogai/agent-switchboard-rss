"""Google News search, RSS/Atom normalization and on-demand collection."""

import asyncio
import calendar
import hashlib
import json
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urlencode, urljoin, urlsplit

import feedparser

from .http import FetchError, UnsafeDestination, validate_url
from .store import Article


class FeedError(RuntimeError):
    pass


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        elif tag in ("br", "p", "div", "li"):
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.hidden:
            self.hidden -= 1
        elif tag in ("p", "div", "li"):
            self.parts.append(" ")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def clean_text(value, limit):
    parser = _Text()
    parser.feed(value or "")
    parser.close()
    text = " ".join("".join(parser.parts).split())
    return text[:limit]


def google_url(query, language="en", region="US"):
    if not isinstance(query, str) or not query.strip() or len(query) > 2000:
        raise ValueError("Invalid Google News query")
    if (not isinstance(language, str) or not isinstance(region, str)
            or not language.replace("-", "").isalpha() or len(language) > 15
            or len(region) != 2 or not region.isascii() or not region.isalpha()):
        raise ValueError("Invalid Google News locale")
    language, region = language.lower(), region.upper()
    return "https://news.google.com/rss/search?" + urlencode(
        {"q": query.strip(), "hl": language, "gl": region, "ceid": f"{region}:{language}"}
    )


def search_source(query, language="en", region="US"):
    url = google_url(query, language, region)
    identity = hashlib.sha256(url.encode()).hexdigest()
    return {"id": "search:" + identity[:12], "name": "Google News", "kind": "google_news",
            "config": {"query": query, "language": language, "region": region},
            "stream": "search:" + identity, "enabled": True}


def article_identity(url):
    parsed = urlsplit(url)
    if parsed.hostname == "news.google.com":
        for prefix in ("/rss/articles/", "/articles/", "/read/"):
            if parsed.path.startswith(prefix):
                token = parsed.path[len(prefix):].strip("/")
                if token and "/" not in token:
                    return "google:" + token
    # Preserve query and fragment: aggressive URL canonicalization can merge
    # distinct stories. Different links may remain duplicates until resolved.
    return "url:" + url


@dataclass(frozen=True)
class ParsedFeed:
    articles: list[Article]
    warnings: list[str]


def parse_feed(body, base_url, *, kind, name, max_entries=2000):
    parsed = feedparser.parse(body, response_headers={"content-location": base_url, "content-type": "application/xml"})
    if not parsed.version or (parsed.bozo and not parsed.entries):
        raise FeedError("Invalid RSS/Atom feed")
    if len(parsed.entries) > max_entries:
        raise FeedError("Feed exceeds entry limit")
    warnings = ["Feed XML was imperfect; parsed available entries"] if parsed.bozo else []
    feed_name = clean_text(parsed.feed.get("title"), 160) or name
    articles = []
    skipped = 0
    for entry in parsed.entries:
        title = clean_text(entry.get("title"), 500)
        url = urljoin(base_url, entry.get("link") or "")
        if not entry.get("link") or not title:
            skipped += 1
            continue
        try:
            validate_url(url)
        except UnsafeDestination:
            skipped += 1
            continue
        publisher = clean_text((entry.get("source") or {}).get("title"), 160) or feed_name
        if kind == "google_news" and title.endswith(" - " + publisher):
            title = title[:-len(" - " + publisher)].strip() or title
        # Google News RSS summaries usually repeat the title and publisher or
        # list related links; they are not actual article descriptions.
        description = None
        if kind != "google_news":
            value = clean_text(entry.get("summary"), 400)
            if value and value.casefold() != title.casefold():
                description = value
        date = entry.get("published_parsed") or entry.get("created_parsed")
        try:
            published = calendar.timegm(date) if date else None
        except (ValueError, OverflowError):
            published = None
        articles.append(Article(article_identity(url), title, publisher, url, published, description))
    if skipped:
        warnings.append(f"Skipped {skipped} entries without usable public links or titles")
    if parsed.entries and not articles:
        raise FeedError("Feed contained entries but none were usable")
    return ParsedFeed(articles, warnings)


class Collector:
    """One shared transport; independent source results and compact stored IDs."""

    def __init__(self, store, http, *, max_sources=30):
        self.store = store
        self.http = http
        self.max_sources = max_sources

    async def refresh(self, sources):
        sources = list({source["stream"]: source for source in sources}.values())
        if len(sources) > self.max_sources:
            raise ValueError("Too many sources in one request")
        return await asyncio.gather(*(self._refresh(source) for source in sources))

    async def _refresh(self, source):
        source_id = source["id"]
        try:
            config = source["config"]
            if source["kind"] == "google_news":
                url = google_url(**config)
            elif source["kind"] == "rss":
                url = config["url"]
            else:
                raise FeedError("Unsupported feed type")
            page = await self.http.fetch(url)
            feed = await asyncio.to_thread(parse_feed, page.body, page.url,
                                           kind=source["kind"], name=source["name"])
            await asyncio.to_thread(self.store.ingest, source["stream"], feed.articles)
            result = {"source": source_id, "stream": source["stream"],
                      "count": len(feed.articles)}
            if feed.warnings:
                result["warnings"] = feed.warnings
            return result
        except (FetchError, FeedError, ValueError) as exc:
            return {"source": source_id, "error": str(exc)}

    async def headlines(self, sources, *, refresh_sources=True, **options):
        if not refresh_sources:
            return await asyncio.to_thread(self.store.headlines, [s["stream"] for s in sources], **options)
        refresh = await self.refresh(sources)
        successful = [result["stream"] for result in refresh if "stream" in result]
        data = await asyncio.to_thread(self.store.headlines, successful, **options)
        errors = {result["source"]: result["error"] for result in refresh if "error" in result}
        warnings = {result["source"]: result["warnings"] for result in refresh if "warnings" in result}
        if errors:
            data["errors"] = errors
        if warnings:
            data["warnings"] = warnings
        if data.get("has_more") and options.get("checkpoint") is None:
            data["page_streams"] = successful
        return data
