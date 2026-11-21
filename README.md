# bottlenecks

[![PyPI](https://img.shields.io/pypi/v/bottlenecks.svg)](https://pypi.org/project/bottlenecks/)
[![Python](https://img.shields.io/pypi/pyversions/bottlenecks.svg)](https://pypi.org/project/bottlenecks/)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

Finds bottlenecks in **code, runtime, pipelines and the host system**. You can ask in Polish or English, type a canonical DSL command, or call it from REST or MCP. Every run is recorded as a [`uritime/urtlog`](https://github.com/uritime/urtlog) event, which gives you history, regressions and scheduled scans. The core has **zero dependencies**.

```text
"pokaż 5 najgorszych wąskich gardeł w kodzie src"        ← NL (PL/EN), Layer 1 / 1.5 / 3
        │ compiles to
code.scan path=src top=5                                  ← canonical DSL (single execution surface)
        │ Engine: registry-validated, effects-checked
LOG URT(at=…, range=…) URI(proc://bottlenecks/code/scan/v1?path=src)
    URN_IN(urn:bottlenecks:input:…) URN_OUT(urn:bottlenecks:report:…, urn:bottlenecks:finding:…) STATUS(succeeded)
```

The architecture follows [`wellmanifest/nl-dsl-llm`](https://github.com/wellmanifest/nl-dsl-llm), [`nl-api-llm`](https://github.com/wellmanifest/nl-api-llm) and [`nl-uri-dsl-llm`](https://github.com/wellmanifest/nl-uri-dsl-llm), with a `uritime/urtlog` record v2 ledger. See the [decision](docs/DECISION/NL_DSL_LLM_URTLOG_ARCHITECTURE.md) and the [architecture](docs/INFORMATION/BOTTLENECKS_ARCHITECTURE.md) docs.

## What it finds

| Action (DSL) | Finds |
|---|---|
| `code.scan` | nested loops, blocking I/O in loops, `+=` string building, regex/sort in loops, complexity, long functions |
| `runtime.profile` / `runtime.pstats` | hot functions, call overhead, recursion (cProfile) |
| `pipeline.analyze` | critical path, choke points, fan-in, serial chains, worker starvation, saturated stages |
| `pipeline.urtlog` | in **recorded executions** (any urtlog `events.jsonl`): slow, failing or tail-latency operations, the lineage critical path, handoff gaps |
| `system.sample` | CPU, run queue, I/O wait, memory, swap, full disks (psutil optional) |
| `history.diff` / `history.trend` / `history.lineage` | new, worsened and resolved bottlenecks between runs; trends; URN lineage |
| `plan.schedule` / `plan.run_due` | recurring scans as urtlog `PLAN` records |

## Install

```bash
pip install bottlenecks            # stdlib only
pip install "bottlenecks[system]"  # + psutil
```

## Use

```bash
# classic subcommands (unchanged)
bottlenecks code src --top 10 --fail-on high
bottlenecks system
bottlenecks pipeline pipeline.json

# natural language → DSL (PL/EN; prints the compiled DSL)
bottlenecks ask "pokaż krytyczne wąskie gardła w kodzie src"
bottlenecks ask "check system bottlenecks"
bottlenecks ask "sprofiluj skrypt app.py" --yes        # process effect needs confirmation

# canonical DSL, autocomplete, REPL
bottlenecks dsl "code.scan path=src top=5 filter:domain=code"
bottlenecks suggest "code"
bottlenecks shell

# history in a urtlog-compatible ledger (opt-in)
export BOTTLENECKS_STATE_DIR=.bottlenecks
bottlenecks code src && bottlenecks code src
bottlenecks history diff --fail-on high                # CI regression gate
bottlenecks plan schedule "code.scan path=src" --every 1h && bottlenecks plan run-due
bottlenecks urtlog ~/.local/share/urtlog              # bottlenecks in any urtlog event log

# other interfaces, same engine
bottlenecks serve-rest --port 8765   # POST /dsl, POST /query, GET /suggest, /schema, /history
bottlenecks serve-mcp                # tools: nl_ask, execute_dsl, suggest_options, list_actions
bottlenecks self-test                # 12 conformance checks
```

### Integration with urtlog and an LLM

- If `URTLOG_STATE_DIR` is set and `uritime-urtlog` is importable, every record is also mirrored into that urtlog store. Mirroring is fail-soft. You can then use `urtlog query --source bottlenecks` and `urtlog logic-tree --op-prefix op/proc/bottlenecks/`.
- The LLM fallback (Layer 3) is off by default. To enable it, set `BOTTLENECKS_LLM_URL` (any OpenAI-compatible `/v1`, e.g. Ollama or OpenRouter), `BOTTLENECKS_LLM_MODEL` and optionally `BOTTLENECKS_LLM_API_KEY`. Its output is JSON-schema constrained and validated against the registry, and successful answers are learned into the semantic cache.

## Python API

```python
from bottlenecks import Engine, Ledger, analyze_code, render

engine = Engine(ledger=Ledger(".bottlenecks"))
res = engine.ask("znajdź wąskie gardła w kodzie src")      # or engine.execute("code.scan path=src")
print(res.meta["canonicalDsl"], res.meta["urtlog"])
print(render(res.report, "text"))

print(render(analyze_code("src/"), "markdown"))            # analyzers are still plain functions
```

## License

Apache-2.0 © Tom Sapletta
