```json
{"id": "architecture", "kind": "information", "version": 1, "created": "2026-11-21", "updated": "2026-11-21", "owner": "semcod/bottlenecks"}
```

# bottlenecks architecture

## Data model
`Bottleneck(domain, kind, location, message, score, severity, suggestion, metrics)`. The `score` is a normalised impact from 0 to 1. If no severity is given, it is derived from the score: critical ≥ 0.85, high ≥ 0.6, medium ≥ 0.35, low ≥ 0.15, otherwise info. A `Report` holds the findings and per-analyzer summaries. Reports can be merged and ranked by score.

## Analyzers
- `bottlenecks.code`: an AST visitor. It tracks loop depth per function and flags work that multiplies with loop depth.
- `bottlenecks.runtime`: profiles with cProfile. It ranks functions by their own-time share of total runtime.
- `bottlenecks.pipeline`: runs a CPM (critical path method) forward/backward pass over a DAG. For linear stages it computes utilisation as ρ = λ / (c / s), where λ is the arrival rate, c the number of workers and s the service time.
- `bottlenecks.system`: takes a host metrics snapshot using psutil or `/proc`. Each threshold maps to a score that ramps from 0.35 at the warning level to 0.9 at the critical level.

## Rendering
`render(report, "text" | "json" | "markdown")`. On the CLI, `--fail-on SEVERITY` returns exit code 1, which makes it usable as a CI gate.
