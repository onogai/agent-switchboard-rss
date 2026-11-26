# Requirements

## Goal

Make recurring news screening cheap and reliable for agents: scan headlines, select relevant articles, then read and verify only those articles. Support both breaking-news monitoring and broader digests without tying the add-on to particular news rules.

## MVP

### Headline collection

- Search Google News RSS using arbitrary queries (including publisher and time operators), language and region.
- Read arbitrary RSS/Atom URLs. Batch independent searches or feeds into a single MCP call.
- Parse and normalize in code. Default screening payload contains a stable short article ID and clean headline, grouped by source when useful. Include a short description only on request and only if it adds information; no HTML, redundant titles, URLs, images or verbose metadata by default. Publication timestamps may be requested.
- Deduplicate identical articles across feeds and searches where reliable identifiers match. Do not attempt semantic event deduplication.
- Support incremental reads by cursor; also allow an explicit initial time window. Distinguish publication time from first observation time. Late-discovered articles must not be lost merely because they were published earlier.
- Independent callers and query scopes must not consume each other's results. Repeated calls with the same cursor are safe; do not advance a consumer's checkpoint implicitly.
- Bound response sizes and expose continuation when results exceed the limit. Distinguish empty results, partial upstream failures, and expired cursors. Never silently advance past unseen results.

### Article reading

- Read a batch of article IDs or public HTTP(S) URLs. Resolve Google News links only when required for selected articles; reuse successful mappings.
- Return clean readable text, title, publisher URL and useful available metadata. Report per-item errors, truncation, unresolved links and access blocks explicitly. Do not return raw HTML or try to bypass paywalls.
- Support per-article and aggregate output limits, bounded parallelism, request timeouts and sensible retry/backoff.
- Prevent requests to local/private network destinations, including redirects and DNS resolution. Bound downloaded content as well as returned content.

### Deployment and operation

- One container, embedded SQLite in installer-owned persistent state. No external database, separate worker, UI, or dedicated scheduler.
- Standard Switchboard add-on manifest, private network, Streamable HTTP with Bearer authentication, health endpoint, and installer-managed secrets. No core changes or manual credential editing.
- Persist article IDs, URL mappings and observation cursors across restarts. Keep bounded history with configurable retention; make expiration explicit.
- Keep tool descriptions and outputs economical. No LLM calls inside the add-on.
- Keep documentation minimal and review it regularly. Add features only for demonstrated use cases.

## Acceptance

- Live smoke test from the intended host: Google News search (including Reuters and multilingual queries), Google URL resolution, and extraction of representative accessible publisher articles. Report failure rates and access blocks, not just successful examples.
- Fixture-based tests for normalization, deduplication, cursor isolation, late arrivals, pagination, retry safety, retention, partial failures and extraction limits.
- Show that screening never resolves every Google URL or fetches article bodies, and does not include URLs by default.
- Measure actual response bytes, estimated tokens, network calls and elapsed time for a representative screening run versus full metadata output.
- Validate clean installation, persistence across restart and MCP discovery through the supported Switchboard installer; no production news rules changed until integration is verified.

## Out of scope

Persistent subscription management, background polling, notifications, news relevance scoring, semantic deduplication, full-text search, UI, and long-term article archiving. Task Queue owns scheduling and agents own editorial decisions. On-demand polling cannot guarantee discovery of every publication: RSS feeds are bounded and Google News is not exhaustive.
