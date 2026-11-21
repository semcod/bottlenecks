---
{
  "schema": "wellmanifest.docs/document/v2",
  "id": "bottlenecks-architecture",
  "kind": "information",
  "version": 2,
  "title": "How bottlenecks turns a request into a recorded analysis",
  "status": "proposed",
  "owner": "semcod/bottlenecks",
  "scope": "repository",
  "updated": "2026-11-21",
  "source_revision": "9d678f17887932d8e4083505ffa3ccfa921d2b9e",
  "priority": "P2",
  "evidence": ["repo://semcod/bottlenecks/src/bottlenecks/engine.py", "repo://semcod/bottlenecks/src/bottlenecks/ledger.py",
               "repo://semcod/bottlenecks/tests/test_architecture.py"]
}
---

# How bottlenecks turns a request into a recorded analysis

<!-- docs:section summary -->
## Cel i rezultat

This is the contract of the modules, the request flow and the stored records.
The rationale is in [the decision](../DECISION/NL_DSL_LLM_URTLOG_ARCHITECTURE.md).

<!-- docs:section details -->
## Zakres i rozwiązanie

```text
NL ─► nl.IntentParser ─► nl.SemanticCache ─► nl.LLMTranslator (optional)
CLI / REST / MCP ─────────────► dsl.DSLCommand ─► engine.Engine.execute
                                       │ registry.ActionRegistry (validate)
                                       ▼
          code · runtime · pipeline · system analyzers ─► models.Report
                                       ▼
                     ledger.Ledger: events.jsonl + ledger.sqlite3
```

- **DSL.** The form is `entity.operation key=value filter:key=value`. Arguments
  `top` and `min_severity` apply to every report action. Filters are `severity`,
  `domain` and `kind`. Unknown actions return `NOT_FOUND`; unknown parameters return
  `VALIDATION_ERROR`.
- **Envelope.** The result is `{success, status, data, errors, meta}`. `status` is
  one of OK, NOT_FOUND, VALIDATION_ERROR, EXECUTION_ERROR, UNAUTHORIZED or CLARIFY.
  `meta` holds `sourceLayer`, `canonicalDsl`, `actionUri` and the urtlog `eventId`.
- **Effects.** Each action declares effects: `read`, `process` (`runtime.profile`)
  or `write` (`plan.*`). `process` and `write` actions need `confirm=true`, or `--yes`
  for NL requests.
- **Ledger record.** One record per run. The input is
  `urn:bottlenecks:input:<sha16>` with an `ni:///sha-256` digest. The outputs are
  `urn:bottlenecks:report:<run>` plus `urn:bottlenecks:finding:<fingerprint>`.
  Fingerprints ignore line numbers, so `history.diff` reports new, worsened and
  resolved findings.
- **Plans.** `plan.schedule` writes a `PLAN`. `plan.run_due` executes the plan
  inside its window, or cancels it as a misfire. It then materialises `at + step`.
- **Events.** `pipeline.urtlog` reads any urtlog `events.jsonl`. It reports slow,
  tail-latency, failing and critical-path operations, and handoff gaps. The
  critical path follows `URN_OUT → URN_IN` lineage.

Schemas: `src/bottlenecks/schemas/*.schema.json`, also served by `GET /schema`.

<!-- docs:section validation -->
## Weryfikacja

`pytest`, `bottlenecks self-test` and `bottlenecks registry` show the live contract.

<!-- docs:section risks -->
## Ryzyka i następny krok

The SQLite projection is disposable and is rebuilt from `events.jsonl`. The NL
lexicon in `profiles/nl-lexicon.json` is data; extend it with tests.
