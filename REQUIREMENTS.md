# Requirements

## Goal

Reduce the cost of recurring news screening. Agents receive compact new headlines, select relevant articles, and verify only selected material.

## MVP

- Manage named sources (Google News query with locale, or RSS/Atom URL) and flat groups through MCP. A source can belong to multiple groups. Enable/disable sources. No UI required.
- List the catalog and read selected sources/groups in one call. Support ad hoc Google News search. Fetch on demand; do not require a subscription registry beyond the catalog or a separate scheduler.
- Default results: short stable article IDs and clean titles grouped by publisher. Optional useful descriptions and publication times. No URLs, HTML, images or redundant metadata by default.
- Deduplicate reliable article identities within a result; never merge different stories based solely on headline similarity. Cross-provider duplicates may remain until URL resolution.
- Named checkpoints stored by the add-on, independent per source and checkpoint name. A read never acknowledges results automatically. Explicit acknowledgement after successful handling advances only the returned prefix, monotonically and idempotently. Interactive reads without checkpoints do not affect them. Late observations must remain eligible even if their publication time is older.
- Initial lookback, bounded pages, explicit continuation, and explicit source failures (including partial failure and expired history). A failed source must not be mistaken for no news.
- Read selected IDs or public HTTP(S) URLs in batches. Resolve Google links lazily; cache resolved URLs. Return clean text and publisher URLs, useful available metadata, and per-item errors/truncation. No raw HTML or paywall bypass.
- Bound network concurrency, timeouts, retry/backoff, downloaded bytes and output size. Reject private/local destinations, including DNS and redirects.
- Single private container, SQLite in installer-managed persistent state, bounded retention, Bearer-authenticated Streamable HTTP and health endpoint. Follow standard Switchboard add-on installation; no Core changes.
- Keep documentation and tool contracts small. Administration tools are intended for the interactive profile via existing Gateway grants. No dependency on Task Queue identifiers or tracing.

## Acceptance

- Tests: catalog CRUD, grouping, source changes, stable IDs, deduplication, checkpoint isolation, first lookback, late arrivals, bounded pages, overlapping reads, repeated acknowledgements, retention and partial failures.
- Live tests from intended host: Reuters and multilingual Google News searches, Google URL resolution and article extraction on representative publishers; report blocks and failures.
- Compare payload bytes, estimated tokens and elapsed time against full-metadata screening. Verify no eager URL resolution or article fetching.
- Validate installer, persistence, MCP discovery and grants before changing production news rules.

## Out of scope

Background polling, notifications, scoring, semantic event deduplication, full-text search, UI and long-term archiving. RSS and Google News are bounded and not exhaustive. Task Queue owns scheduling; agents own editorial decisions.
