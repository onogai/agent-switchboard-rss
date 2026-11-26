# Architecture

## Boundaries

```text
Agent / Task Queue -> Switchboard Gateway -> RSS MCP
                                            |-- Google News RSS / RSS / Atom
                                            |-- URL resolver + article extractor
                                            '-- SQLite (persistent volume)
```

One independently deployable Python service. Switchboard owns routing, grants, discovery and activity. Task Queue owns scheduling; agents own relevance, event deduplication, verification and delivery. The add-on owns fetching, normalization, article identity, observation history and bounded output.

Use FastMCP, an async HTTP client, feedparser, a maintained Google News URL decoder and Trafilatura or an equivalent extractor. Validate the libraries against live sources before committing to them; no copied upstream server implementation. Keep blocking library work off the async event loop.

## Agent interface

Three conceptual tools; exact schemas may evolve during implementation:

| Tool | Purpose |
| --- | --- |
| `search_news` | Batch Google News queries with locale and incremental filters. |
| `read_feeds` | Batch arbitrary RSS/Atom feeds with incremental filters. |
| `read_articles` | Batch article IDs or URLs, resolve redirects and extract text. |

Illustrative screening response, not a frozen wire schema:

```json
{
  "streams": [
    {
      "key": "reuters",
      "next_cursor": "opaque-cursor",
      "has_more": false,
      "sources": {
        "Reuters": [[1241, "ECB signals possible rate cut"], [1242, "Oil prices rise"]]
      }
    }
  ]
}
```

The agent can then call `read_articles(ids=[1241, 1242])`. URLs remain internal during screening; original publisher URLs are returned when reading for citation. Use a compact structured response, not duplicated verbose representations.

## State and incremental reads

Use SQLite, not a separate database service. Store normalized entries with stable short IDs, original feed links, optional resolved publisher URLs, publication and observation timestamps, and optional useful descriptions. Keep stream membership/observations separate from article identity: the same article may appear in multiple queries at different times.

An incremental cursor is scoped to a normalized feed/query (including locale) and tracks the observation sequence for that scope, not the publication timestamp or a global article ID. A newly observed membership for an existing article must remain visible to that stream. Exact identity matching should be conservative (feed GUID scoped to publisher/feed, stable Google article identifier, or reliable URL); never merge stories merely because headlines match.

Each call refreshes the requested streams on demand and reads the bounded result from local state. Return continuation for each stream; page in observation order with a stable upper watermark when needed. Do not skip entries to reach the newest page. An initial lookback is explicit and bounded; subsequent reads use the returned cursor. Checkpoints belong to the caller and are saved only after successful handling. No server-side auto-ack. If retained history cannot satisfy a cursor, report that and require explicit resynchronization. A failed refresh must be visible even if cached entries are available.

Persist state across restarts; prune on demand using configurable retention. Cache resolved URLs. Do not persist full article bodies in MVP unless a measured need justifies it. No subscription registry or background scheduler is required to support stream identity.

## Fetching and extraction

Headline collection must not resolve Google redirects by default or download article bodies. Resolve and fetch only selected articles. Use bounded concurrency shared across requests, deadlines, retry/backoff and per-item results; stop on the configured aggregate output budget without hiding unprocessed items. Validate public destinations and redirects at the HTTP boundary, bound input sizes, and never pass raw HTML to agents. Extraction failures are explicit; native web remains a useful fallback.

## Packaging

Follow the current [Switchboard add-on contract](https://github.com/onogai/agent-switchboard/blob/main/ADDONS.md) at implementation time: one private Compose service, installer-owned data and credentials, Bearer-authenticated MCP and health endpoint. Use schema v2 where supported and remain compatible with the active production installation and the evolving declarative discovery design. No host port, no core changes, no add-on-specific installer logic. Do not invent a second discovery mechanism.

Keep the implementation and documentation small. Add abstractions, extra tools and persistent caches only when a concrete requirement warrants them.

## Open decisions

- Confirm Google URL decoder reliability and extraction quality from the actual host before finalizing dependencies.
- Choose cursor serialization, retention default, and response/concurrency limits based on tests and payload measurements.
- Confirm current installer/discovery contract before implementing the manifest.
