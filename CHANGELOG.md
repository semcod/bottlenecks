# Changelog

## [Unreleased]

## [0.2.0] - 2026-11-21

### Added
- Requests in natural language (Polish and English), canonical DSL, CLI, REST and MCP all run through one validated engine — [decision](docs/DECISION/NL_DSL_LLM_URTLOG_ARCHITECTURE.md).
- Every run is recorded as a `uritime/urtlog` record v2 (`LOG`/`PLAN` 4-tuple). This enables run history, regression diffs, trends, URN lineage and scheduled scans — [architecture](docs/INFORMATION/BOTTLENECKS_ARCHITECTURE.md).
- `bottlenecks urtlog <state>` finds bottlenecks in recorded executions: slow, failing or tail-latency operations, the critical path and handoff gaps.
- `bottlenecks self-test` runs 12 conformance checks.
- Native `wellmanifest/new-project 0.20.166` repository governance with `-p wellmanifest_governance` pytest gate.

### Changed
- `bottlenecks.__version__` resolves dynamically from the repository `VERSION` file in source checkouts and from `importlib.metadata` in installed distributions.

## [0.1.0] - 2026-11-21

### Docs
- Update CHANGELOG.md
- Update README.md
- Update docs/README.md
- Update docs/information/architecture.md

### Test
- Update tests/test_bottlenecks.py

### Other
- Update .env.example
- Update .gitignore
- Update LICENSE
- Update VERSION
- Update uv.lock


## [0.1.0] - 2026-11-21

### Added
- `code` analyzer (AST): nested-loops, io-in-loop, str-concat-in-loop, regex-in-loop, sort-in-loop, high-complexity, long-function.
- `runtime` analyzer (cProfile): `profile()` context manager, `profile_callable`, `profile_script`, `load_pstats`.
- `pipeline` analyzer: critical path, slack, choke points, fan-in, serial chains, worker starvation, stage utilisation.
- `system` analyzer: CPU, load, I/O wait, memory, swap, disk; psutil optional with `/proc` fallback.
- Text, JSON and Markdown renderers; `bottlenecks` CLI with `--fail-on` CI gate.
