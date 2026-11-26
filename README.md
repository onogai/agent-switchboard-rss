# Switchboard RSS

A lightweight, agent-oriented news and RSS MCP add-on for [Switchboard](https://github.com/onogai/agent-switchboard).

Screen new headlines cheaply, select relevant articles, and read only those articles. No LLM inside the service.

## MVP

- Named Google News searches and RSS/Atom sources, grouped and managed through MCP.
- Compact batch headline results (short IDs and titles; no URLs by default).
- Independent named checkpoints, acknowledged only after successful processing.
- Ad hoc Google News search and batch article reading with lazy URL resolution.
- One container, SQLite, authenticated Streamable HTTP; no UI, external database or background scheduler.

See [Requirements](REQUIREMENTS.md) and [Architecture](ARCHITECTURE.md).

## Status

Initial SQLite catalog, checkpoints, Google News/RSS collection and compact normalization. Batch article reading uses googlenewsdecoder and Trafilatura. Seven MCP tools and a private Bearer-authenticated HTTP entry point are implemented. Retention, packaging and live validation remain open.

Install dependencies with `python -m pip install -r requirements.txt`; run tests with `python -m unittest discover -s tests -v`.

Run locally with `python -m rss_addon.server` (loopback by default, port 8775). For non-loopback binding set `RSS_BIND_HOST` and `RSS_MCP_TOKEN_FILE` to a private token file. `RSS_DB` selects the SQLite path.

## References

- [google-rss-mcp](https://github.com/ccw7463/google-rss-mcp) — functional reference only, not a fork or dependency.
- [feedparser](https://github.com/kurtmckee/feedparser) — RSS/Atom parsing candidate.
- [googlenewsdecoder](https://github.com/SSujitX/google-news-url-decoder) — Google News URL resolution (pinned protocol helpers).
- [Trafilatura](https://github.com/adbar/trafilatura) — article extraction.

Independent implementation. Prefer maintained libraries, review their licenses and retain required notices.
