# Verimend

> Verify reality, mend the docs.

Verimend crawls the Spirrow products, detects drift between reality (code, config, running services) and their documentation, and proposes fixes as pull requests.

[日本語版 README](README.ja.md)

## Name

**verify + mend.** By running verification and mending every night, the documentation spirals ever closer to reality.

## How it works

1. **Fact collection** — Repositories, MCP tool definitions, config, and service_health are gathered by deterministic scripts (no LLM involved)
2. **Claim extraction** — Documents are decomposed into atomic claims with source anchors (LLM: Qwen3.8-27B via Lexora)
3. **Reconciliation** — Each claim is matched against fact snippets and classified as `verified / stale / unverifiable`
4. **Mending** — Stale claims get draft patches, bundled into one PR per crawl. Unverifiable claims and large-scale drift are escalated to a chatroom thread for human judgment

## Design principles

- **Route by verifiability**: only fixes backed by deterministic checks may be auto-applied; anything involving LLM judgment stops at a PR (humans merge)
- **Don't rely on long context**: reconciliation runs on small, retrieval-narrowed units
- **Delegate the periphery to the existing platform**: LLM inference = Lexora / document retrieval = Prismind / GitHub operations = Magickit / escalation = Conclair chatroom

## Status

Implementation, milestone M1. The service scaffold is in place: FastAPI on :8118 with `/health`,
SQLite migrations for `crawl_run` / `fact`, and crawl targets declared in
[config/targets.yaml](config/targets.yaml). The collector, extractor, reconciler, and mender are not
built yet. See [docs/design.md](docs/design.md) (Japanese).
