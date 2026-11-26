# Switchboard RSS

A lightweight, agent-oriented news and RSS MCP add-on for [Switchboard](https://github.com/onogai/agent-switchboard).

The goal is to reduce the cost of news screening: fetch and normalize headlines in code, give agents only new and compact results, and read original articles only when selected.

## Planned MVP

- Google News RSS search by query, language, and region, plus arbitrary RSS/Atom feeds.
- Batch headline screening with stable short article IDs and incremental cursors.
- Batch reading of selected articles, resolving Google News links lazily and extracting readable text without an LLM.
- One self-hosted container with embedded SQLite; no external database, UI, or separate scheduler.
- Standard Switchboard add-on installation and authenticated Streamable HTTP MCP.

See [Requirements](REQUIREMENTS.md) and [Architecture](ARCHITECTURE.md).

## Status

Design only. No runnable server or installation package yet.

## References

- [google-rss-mcp](https://github.com/ccw7463/google-rss-mcp) — functional reference, not a fork or dependency.
- [feedparser](https://github.com/kurtmckee/feedparser) — RSS/Atom parsing candidate.
- [googlenewsdecoder](https://github.com/SSujitX/google-news-url-decoder) — Google News URL resolution candidate.
- [Trafilatura](https://github.com/adbar/trafilatura) — article text extraction candidate.

This is an independent implementation. Prefer maintained libraries over custom parsers; review their licenses and preserve required notices. Revisit reference projects when useful, especially if Google changes its redirect format.
