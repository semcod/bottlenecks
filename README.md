# bottlenecks

[![PyPI](https://img.shields.io/pypi/v/bottlenecks.svg)](https://pypi.org/project/bottlenecks/)
[![Python](https://img.shields.io/pypi/pyversions/bottlenecks.svg)](https://pypi.org/project/bottlenecks/)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

Finds bottlenecks in four places, and ranks them all in one report:

| Domain     | What it finds | How |
|------------|---------------|-----|
| `code`     | nested loops (O(n^k)), blocking I/O in loops, `+=` string building, regex/sort in loops, high complexity, long functions | static AST analysis |
| `runtime`  | hot functions, call-overhead hotspots, recursion | stdlib `cProfile` |
| `pipeline` | critical path, choke points, join barriers (fan-in), serial chains, worker starvation, saturated queue stages | DAG and queueing math |
| `system`   | CPU saturation, run queue/load, I/O wait, memory pressure, swapping, full disks | `psutil` (optional) or `/proc` |

Each finding is a `Bottleneck` with a `domain`, a `kind`, a `location`, a `score` from 0 to 1, a `severity`, a `suggestion` and raw `metrics`.

The core has **no dependencies**. To get richer system data, including top processes, install the `psutil` extra.

## Install

```bash
pip install bottlenecks            # core, stdlib only
pip install "bottlenecks[system]"  # + psutil
```

## CLI

```bash
bottlenecks code src/ --top 20                  # static analysis
bottlenecks code src/ --fail-on high            # CI gate (exit 1)
bottlenecks run script.py arg1 arg2             # profile a script
bottlenecks pstats out.prof                     # analyse a saved profile
bottlenecks pipeline pipeline.json -f markdown  # DAG / stage analysis
bottlenecks system --interval 1                 # host resources
bottlenecks scan .                              # code + system
```

Output formats: `text` (default), `json`, `markdown`. Use `-o FILE` to write the report to a file.

### Pipeline spec

```json
{
  "workers": 4,
  "tasks": [
    {"name": "fetch",   "duration": 2},
    {"name": "build",   "duration": 10, "deps": ["fetch"]},
    {"name": "lint",    "duration": 1,  "deps": ["fetch"]},
    {"name": "test",    "duration": 5,  "deps": ["build"]},
    {"name": "publish", "duration": 1,  "deps": ["test", "lint"]}
  ],
  "arrival_rate": 2.5,
  "stages": [
    {"name": "parse", "service_time": 0.1},
    {"name": "embed", "service_time": 1.0, "workers": 2},
    {"name": "store", "service_time": 0.2}
  ]
}
```

## Python API

```python
from bottlenecks import analyze_code, profile, analyze_dag, analyze_system, render

report = analyze_code("src/")
report.merge(analyze_system())
print(render(report, "text", top=10))

with profile(threshold=0.02) as p:
    expensive()
print(render(p.report))

print(analyze_dag(tasks).summary["pipeline"]["critical_path"])
```

## License

Licensed under Apache-2.0.
