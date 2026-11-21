---
{
  "schema": "wellmanifest.docs/document/v2",
  "id": "nl-dsl-llm-urtlog-architecture",
  "kind": "decision",
  "version": 1,
  "title": "bottlenecks adopts the nl-* tripartite architecture and the urtlog record ledger",
  "status": "proposed",
  "owner": "semcod/bottlenecks",
  "scope": "repository",
  "updated": "2026-11-21",
  "source_revision": "9d678f17887932d8e4083505ffa3ccfa921d2b9e",
  "priority": "P2",
  "evidence": [
    "github://wellmanifest/nl-dsl-llm/commit/f4f1e268584c86a044313f5883dfb921a7f5ac38",
    "github://wellmanifest/nl-api-llm/commit/f6b0b26e6c12d80086af376e67326d7bc038417b",
    "github://wellmanifest/nl-uri-dsl-llm/commit/29a67748a41bec2ccd600a5c978c7caa5d362450",
    "github://uritime/urtlog/commit/3c7674b4cab1995b12932102d3aec1a70d4f3c03",
    "repo://semcod/bottlenecks/tests/test_architecture.py"
  ]
}
---

# bottlenecks adopts the nl-* tripartite architecture and the urtlog record ledger

<!-- docs:section summary -->
## Cel i rezultat

Version 0.1.0 exposed analyzers only through an ad-hoc CLI and kept no history.
We restructure the library around the `wellmanifest/nl-*` pattern: NL is an intake,
the canonical DSL is the only execution surface, and interfaces stay in parity.
Every execution is recorded as a `uritime/urtlog` record v2 4-tuple.

<!-- docs:section details -->
## Zakres i rozwiązanie

Decision, by source standard:

- **nl-dsl-llm.** Layer 1 is a deterministic PL/EN fast path. Layer 1.5 is a semantic
  cache with polarity and slot guards. Layer 3 is an optional LLM translator,
  JSON-schema constrained and fail-closed. Layer 2 is `Engine.execute`, the single
  validated execution boundary. Layer 0.5 is the option network. CLI, REST and MCP
  call the same engine.
- **nl-api-llm.** One action registry is the single source of truth. Each entry
  carries parameters, effects and NL intents, and is exported as an `api-registry`
  manifest.
- **nl-uri-dsl-llm.** Every action has an Action URI,
  `proc://bottlenecks/<entity>/<operation>/v1`. Results are Resource URNs:
  `urn:bottlenecks:{input,report,finding}:…`.
- **urtlog.** Each run is one `LOG URT() URI() URN_IN() URN_OUT() STATUS()` record.
  Records go into an append-only `events.jsonl` plus a SQLite projection over
  `time_path`, `op_path` and `res_path`. Scheduled scans are `PLAN` records,
  settled causally (`causation_id`) by an idempotent `run_due`. Records are mirrored
  into a real urtlog store when `URTLOG_STATE_DIR` is set.

Alternatives rejected:

- A hard dependency on `uritime-urtlog` (not on PyPI, Python 3.11+ only).
- A dependency on the `wellmanifest-nl-dsl-llm` package (pulls in grpc/pydantic).
- A private history format, which loses lineage with heyasa, planfile and diagit.

Not adopted: gRPC and the WebSocket transport (CONF-04/08). They remain gaps.

<!-- docs:section validation -->
## Weryfikacja

- `pytest` passes 43 tests on Python 3.9, 3.10 and 3.13.
- `bottlenecks self-test` passes 12/12 checks. With `uritime/urtlog` on `PYTHONPATH`,
  the self-test re-parses every record with `urtlog.dsl.parse_dsl_line`.
- A manual run with `URTLOG_STATE_DIR` showed the records in `urtlog query` and
  `urtlog logic-tree`.

Gaps: no protected CI, and no Validator review of this change yet.

<!-- docs:section risks -->
## Ryzyka i następny krok

Classic CLI subcommands and the JSON report output stay compatible. History is
opt-in: nothing is written without `--state` or `BOTTLENECKS_STATE_DIR`. Actions
with `process` or `write` effects need explicit confirmation when they come from
NL, REST or MCP.

Rollback: revert the PR; the analyzers are unchanged.

Next steps:

- Adopt `.governance/docs.json` and the new-project governance.
- Configure the OneDev/Validator profile before release 0.2.0.
