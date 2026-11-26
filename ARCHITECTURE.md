# Architecture

Agent → Switchboard Gateway → RSS MCP → Google News / RSS / publisher sites.
The add-on owns its SQLite database. No Core or Task Queue dependency.

## Tools

| Tool | Purpose |
| --- | --- |
| `list_sources` | Compact catalog of sources and groups. |
| `get_headlines` | Refresh named sources/groups and return compact headlines. Optional named checkpoint and initial lookback. |
| `search_news` | Ad hoc batch Google News search. |
| `read_articles` | Read selected article IDs or URLs. |
| `ack_headlines` | Acknowledge a successfully handled batch. |
| `manage_sources` | Create/update/remove/enable/disable sources. |
| `manage_groups` | Create/update/remove flat groups and memberships. |

Grant the last two only to interactive profiles using existing Switchboard grants. Exact schemas may evolve. Ordinary reads can test sources without advancing a checkpoint.

Example: `get_headlines(groups=["breaking"], checkpoint="news-breaking", since="2h")` returns publisher-grouped rows like `[1241, "Headline"]`, a batch ID, continuation flag and source errors when applicable. Then `read_articles(ids=[1241])` returns clean text and publisher URL. Acknowledge the batch only after processing and any required delivery. A read with no checkpoint is stateless with respect to acknowledgement.

## State

Named sources and group memberships are persisted. Sources have an internal stream identity; changing the feed/query/locale creates a new stream rather than reusing the old checkpoint. Enable/name changes preserve it. Article identity is separate from stream observations. Use conservative identifiers supplied by the normalization layer; short article IDs remain stable while retained.

Each named checkpoint tracks observation sequence per stream, not publication time or global article ID. On first use, capture an initial publication/observation lookback and observation watermark. Later observations are eligible even when published before that window. Read pages in observation order; acknowledge only the prefix represented by a batch. Acknowledgements are monotonic and idempotent, including overlapping reads. Unacknowledged entries may be repeated. A newly added source starts independently. Same article can appear again in another page/stream; deduplicate within a page, not semantically.

Prune old history with explicit checkpoint-expiry behavior before production. Cache resolved URLs; do not cache full article bodies without a measured need. Keep checkpoint names independent of task IDs and rule IDs. No implicit acknowledgement and no separate consumer registry.

## Fetching and deployment

Use FastMCP, bounded asynchronous HTTP, feedparser, a maintained Google URL decoder and Trafilatura or equivalent. Test against live sources before settling dependencies. Validate every destination and redirect, including DNS, and avoid blocking the event loop. Failures must remain distinguishable from empty results. Native web is a fallback.

Follow the current [Switchboard add-on contract](https://github.com/onogai/agent-switchboard/blob/main/ADDONS.md): private Compose service, installer-owned data and credentials, Bearer-authenticated MCP, health endpoint and standard discovery. No host ports or custom installer logic.

Keep code and documentation minimal; review regularly. Retention policy, network limits and final schemas remain implementation decisions.
