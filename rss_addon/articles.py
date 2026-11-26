"""Read selected articles, never HTML or eager publisher URL resolution."""

import asyncio
import re
from urllib.parse import urlsplit

from googlenewsdecoder._parse import (
    BATCHEXECUTE_URL, article_id, build_batchexecute_body,
    params_page_url, parse_batchexecute, parse_signature,
)
from trafilatura import bare_extraction

from .http import FetchError, HTTPError, UnsafeDestination, validate_url


def is_google(url):
    return urlsplit(url).hostname == "news.google.com"


class GoogleResolver:
    """Use the pinned decoder's protocol helpers with our bounded transport.

    The package's standalone HTTP client would bypass our destination and
    download limits. Keep its private helper API isolated in this module.
    """

    def __init__(self, http):
        self.http = http

    async def _params(self, index, url):
        try:
            validate_url(url)
            token = article_id(url)
            if not token or not re.fullmatch(r"[A-Za-z0-9_-]{1,4096}", token):
                raise FetchError("Invalid Google News article link")
            page = await self.http.fetch(params_page_url(token, url),
                                         allowed_host="news.google.com")
            signature = parse_signature(page.body.decode("utf-8", errors="replace"))
            if not signature:
                raise FetchError("Google News decoding parameters unavailable")
            sig, timestamp = signature
            return (str(index), token, timestamp, sig)
        except (FetchError, ValueError) as exc:
            return str(exc)

    async def resolve(self, urls):
        if not urls:
            return []
        params = await asyncio.gather(*(self._params(i, url) for i, url in enumerate(urls)))
        result = [(None, value) if isinstance(value, str) else (None, "Google URL unresolved")
                  for value in params]
        pending = [value for value in params if not isinstance(value, str)]
        if not pending:
            return result
        try:
            page = await self.http.fetch(
                BATCHEXECUTE_URL, method="POST", data=build_batchexecute_body(pending),
                headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
                allowed_host="news.google.com",
            )
            pairs = parse_batchexecute(page.body.decode("utf-8", errors="replace"))
            # Never assign a partial or reordered unidentified response to the
            # wrong article. One pending request is unambiguous.
            by_id = {}
            ambiguous = set()
            for key, url in pairs:
                if key is not None:
                    if key in by_id and by_id[key] != url:
                        ambiguous.add(key)
                    by_id[key] = url
            for index, *_ in pending:
                url = by_id.get(index)
                if url is None and len(pending) == len(pairs) == 1:
                    url = pairs[0][1]
                if not url or index in ambiguous:
                    continue
                try:
                    validate_url(url)
                    if is_google(url):
                        raise UnsafeDestination("Google returned an unresolved link")
                    result[int(index)] = (url, None)
                except (FetchError, ValueError):
                    result[int(index)] = (None, "Invalid decoded publisher URL")
        except (FetchError, ValueError, TypeError, IndexError, KeyError) as exc:
            for index, *_ in pending:
                result[int(index)] = (None, "Google URL resolution failed: " + str(exc)[:200])
        return result


def extract_article(page):
    doc = bare_extraction(page.body, url=page.url, with_metadata=True,
                          include_comments=False, include_links=False,
                          include_images=False, favor_precision=True)
    if doc is None or not doc.text or not doc.text.strip():
        raise FetchError("No readable article text")
    result = {"text": doc.text.strip()}
    for field, limit in (("title", 500), ("author", 200), ("date", 80)):
        value = getattr(doc, field, None)
        if isinstance(value, str) and value.strip():
            result[field] = value.strip()[:limit]
    return result


class ArticleReader:
    def __init__(self, store, http, *, extractor=extract_article):
        self.store = store
        self.http = http
        self.resolver = GoogleResolver(http)
        self.extractor = extractor
        self._extract_slots = asyncio.Semaphore(2)

    async def read(self, ids=(), urls=(), *, max_chars_per_article=8000,
                   max_total_chars=40000):
        ids, urls = list(dict.fromkeys(ids)), list(dict.fromkeys(urls))
        if not 1 <= len(ids) + len(urls) <= 20:
            raise ValueError("Choose 1-20 article IDs or URLs")
        if not 200 <= max_chars_per_article <= 30000 or not 200 <= max_total_chars <= 120000:
            raise ValueError("Invalid article text budget")
        items = []
        for article in ids:
            record = await asyncio.to_thread(self.store.article, article)
            if record is None:
                items.append({"id": article, "error": "Unknown or expired article ID"})
                continue
            item = {"id": article, "title": record["title"], "source": record["publisher"],
                    "url": record["resolved_url"] or record["url"]}
            if record["published"] is not None:
                item["published"] = record["published"]
            items.append(item)
        items.extend({"url": url} for url in urls)
        pending = {}
        for i, item in enumerate(items):
            if "error" in item:
                continue
            try:
                validate_url(item["url"])
                if is_google(item["url"]):
                    pending.setdefault(item["url"], []).append(i)
            except (FetchError, ValueError) as exc:
                item["error"] = str(exc)
        decoded = await self.resolver.resolve(list(pending))
        for (google_url, indices), (url, error) in zip(pending.items(), decoded):
            for index in indices:
                item = items[index]
                if error:
                    item["error"] = error
                else:
                    item["url"] = url
                    if "id" in item:
                        await asyncio.to_thread(self.store.remember_url, item["id"], url)
        # Group identical publisher URLs so selected duplicates cost one fetch.
        targets = {}
        for item in items:
            if "error" not in item:
                targets.setdefault(item["url"], []).append(item)
        # Limit the extracted text before accumulating batch output.
        results = await asyncio.gather(*(self._read_one(url, max_chars_per_article)
                                         for url in targets))
        for (_, members), result in zip(targets.items(), results):
            for item in members:
                item.update(result)
        remaining = max_total_chars
        for item in items:
            text = item.get("text")
            if text is None:
                continue
            if remaining == 0:
                item.pop("text")
                item["error"] = "Total text budget exhausted; retry this article"
                item["truncated"] = True
                continue
            if len(text) > remaining:
                item["text"] = text[:remaining]
                item["truncated"] = True
            remaining -= len(item["text"])
        return {"articles": items}

    async def _read_one(self, url, limit):
        try:
            page = await self.http.fetch(url)
            if is_google(page.url):
                raise FetchError("Publisher URL redirected back to Google News")
            async with self._extract_slots:
                result = await asyncio.to_thread(self.extractor, page)
            text = result["text"]
            result["truncated"] = len(text) > limit
            result["text"] = text[:limit]
            result["url"] = page.url
            return result
        except HTTPError as exc:
            return {"error": (f"Publisher access blocked (HTTP {exc.status})"
                              if exc.status in (401, 402, 403, 451) else str(exc))}
        except (FetchError, ValueError) as exc:
            return {"error": str(exc)}
