"""Pipeline / DAG / queue bottleneck detection.

Two models are supported:

* **DAG of tasks** – ``[{"name": "build", "duration": 12, "deps": ["fetch"]}, ...]``.
  Computes the critical path, slack, parallelism and fan-in/fan-out choke points.
* **Stage pipeline** – ``[{"name": "parse", "service_time": 0.2, "workers": 2}, ...]``
  with an ``arrival_rate`` (items / second). Computes utilisation (Little / M/M/c
  style ``rho``) and the throughput-limiting stage.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .models import Bottleneck, Report


class CycleError(ValueError):
    """Raised when the task graph contains a cycle."""


def _normalise(tasks: Sequence[Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
    graph: Dict[str, Dict[str, Any]] = {}
    for t in tasks:
        name = str(t["name"])
        graph[name] = {"duration": float(t.get("duration", 0.0)), "deps": [str(d) for d in t.get("deps", [])]}
    for name, t in graph.items():
        for d in t["deps"]:
            if d not in graph:
                raise ValueError(f"task '{name}' depends on unknown task '{d}'")
    return graph


def _toposort(graph: Dict[str, Dict[str, Any]]) -> List[str]:
    indeg = {n: len(t["deps"]) for n, t in graph.items()}
    children: Dict[str, List[str]] = {n: [] for n in graph}
    for n, t in graph.items():
        for d in t["deps"]:
            children[d].append(n)
    ready = sorted(n for n, k in indeg.items() if k == 0)
    order: List[str] = []
    while ready:
        n = ready.pop(0)
        order.append(n)
        for c in sorted(children[n]):
            indeg[c] -= 1
            if indeg[c] == 0:
                ready.append(c)
    if len(order) != len(graph):
        raise CycleError("task graph contains a cycle: " + ", ".join(sorted(set(graph) - set(order))))
    return order


def critical_path(tasks: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Return earliest/latest start, slack and the critical path of a task DAG."""
    graph = _normalise(tasks)
    order = _toposort(graph)
    children: Dict[str, List[str]] = {n: [] for n in graph}
    for n, t in graph.items():
        for d in t["deps"]:
            children[d].append(n)
    es: Dict[str, float] = {}
    for n in order:
        es[n] = max((es[d] + graph[d]["duration"] for d in graph[n]["deps"]), default=0.0)
    makespan = max((es[n] + graph[n]["duration"] for n in graph), default=0.0)
    ls: Dict[str, float] = {}
    for n in reversed(order):
        lf = min((ls[c] for c in children[n]), default=makespan)
        ls[n] = lf - graph[n]["duration"]
    slack = {n: round(ls[n] - es[n], 9) for n in graph}
    # walk the critical chain from a zero-slack root following zero-slack successors
    path: List[str] = []
    current: Optional[str] = None
    roots = [n for n in order if not graph[n]["deps"] and slack[n] == 0]
    if roots:
        current = max(roots, key=lambda n: graph[n]["duration"])
    while current is not None:
        path.append(current)
        nxt = [c for c in children[current]
               if slack[c] == 0 and abs(es[c] - (es[current] + graph[current]["duration"])) < 1e-9]
        current = max(nxt, key=lambda n: graph[n]["duration"]) if nxt else None
    total_work = sum(t["duration"] for t in graph.values())
    return {
        "makespan": makespan,
        "total_work": total_work,
        "parallelism": (total_work / makespan) if makespan else 0.0,
        "critical_path": path,
        "earliest_start": es,
        "slack": slack,
        "children": children,
        "graph": graph,
    }


def analyze_dag(tasks: Sequence[Mapping[str, Any]], workers: Optional[int] = None) -> Report:
    """Detect bottlenecks in a DAG of tasks with durations and dependencies."""
    cp = critical_path(tasks)
    graph, children, makespan = cp["graph"], cp["children"], cp["makespan"]
    report = Report()
    for name in cp["critical_path"]:
        dur = graph[name]["duration"]
        share = dur / makespan if makespan else 0.0
        if share >= 0.1:
            report.items.append(Bottleneck(
                "pipeline", "critical-task", name,
                f"on the critical path, {share:.0%} of makespan ({dur:g} of {makespan:g})",
                min(1.0, 0.2 + share), suggestion="speed up, split or cache this task – it directly bounds total time",
                metrics={"duration": dur, "share": round(share, 4)}))
    total = len(graph)
    for name, kids in children.items():
        descendants = _descendants(name, children)
        if total > 3 and len(descendants) / (total - 1) >= 0.5 and len(kids) >= 2:
            ratio = len(descendants) / (total - 1)
            report.items.append(Bottleneck(
                "pipeline", "choke-point", name,
                f"blocks {len(descendants)} of {total - 1} downstream tasks ({len(kids)} direct)",
                min(1.0, 0.15 + 0.5 * ratio),
                suggestion="make it fail fast, isolate failures and start independent branches early",
                metrics={"descendants": len(descendants), "direct_children": len(kids)}))
        deps = graph[name]["deps"]
        if len(deps) >= 4:
            report.items.append(Bottleneck(
                "pipeline", "fan-in", name, f"waits for {len(deps)} upstream tasks (join barrier)",
                min(0.8, 0.1 + 0.05 * len(deps)), suggestion="reduce dependencies or allow partial/streaming joins",
                metrics={"deps": len(deps)}))
    parallelism = cp["parallelism"]
    if total >= 3 and parallelism < 1.3:
        report.items.append(Bottleneck(
            "pipeline", "serial-chain", " -> ".join(cp["critical_path"]),
            f"pipeline is essentially serial (parallelism {parallelism:.2f})", 0.5,
            suggestion="break artificial dependencies so independent tasks can run concurrently",
            metrics={"parallelism": round(parallelism, 3)}))
    if workers and parallelism > workers * 1.5:
        report.items.append(Bottleneck(
            "pipeline", "worker-starvation", f"workers={workers}",
            f"available parallelism {parallelism:.1f} far exceeds {workers} workers", 0.45,
            suggestion="add worker slots or prioritise critical-path tasks",
            metrics={"parallelism": round(parallelism, 3), "workers": workers}))
    report.summary["pipeline"] = {
        "tasks": total, "makespan": makespan, "total_work": cp["total_work"],
        "parallelism": round(parallelism, 3), "critical_path": cp["critical_path"],
    }
    return report


def _descendants(name: str, children: Dict[str, List[str]]) -> set:
    seen: set = set()
    stack = list(children[name])
    while stack:
        n = stack.pop()
        if n not in seen:
            seen.add(n)
            stack.extend(children[n])
    return seen


def analyze_stages(stages: Sequence[Mapping[str, Any]], arrival_rate: Optional[float] = None) -> Report:
    """Detect the throughput-limiting stage of a linear pipeline.

    Each stage: ``name``, ``service_time`` (seconds per item) and optional ``workers``.
    """
    report = Report()
    caps = []
    for s in stages:
        workers = max(1, int(s.get("workers", 1)))
        st = float(s["service_time"])
        capacity = workers / st if st > 0 else float("inf")
        caps.append((str(s["name"]), capacity, workers, st))
    if not caps:
        return report
    throughput = min(c[1] for c in caps)
    rate = arrival_rate if arrival_rate is not None else throughput
    for name, capacity, workers, st in caps:
        rho = rate / capacity if capacity != float("inf") else 0.0
        is_limit = capacity == throughput
        if rho >= 0.7 or is_limit:
            score = min(1.0, rho) if arrival_rate is not None else (0.6 if is_limit else rho * 0.6)
            msg = f"utilisation {rho:.0%} (capacity {capacity:.2f}/s with {workers} worker(s))"
            if rho >= 1:
                msg += " – queue grows without bound"
            report.items.append(Bottleneck(
                "pipeline", "saturated-stage" if rho >= 0.85 else "limiting-stage", name, msg, score,
                suggestion="add workers, reduce service time or buffer/batch upstream",
                metrics={"utilisation": round(rho, 4), "capacity": capacity, "workers": workers, "service_time": st}))
    report.summary["stages"] = {"throughput": throughput, "arrival_rate": rate,
                                "limiting_stage": min(caps, key=lambda c: c[1])[0]}
    return report


def analyze_pipeline(spec: Mapping[str, Any]) -> Report:
    """Analyse a JSON-like spec containing ``tasks`` and/or ``stages``."""
    report = Report()
    if "tasks" in spec:
        report.merge(analyze_dag(spec["tasks"], workers=spec.get("workers")))
    if "stages" in spec:
        report.merge(analyze_stages(spec["stages"], arrival_rate=spec.get("arrival_rate")))
    if not report.summary:
        raise ValueError("pipeline spec must contain 'tasks' and/or 'stages'")
    return report


# --------------------------------------------------------------------------
# Bottlenecks in *recorded executions* (uritime/urtlog record v2 event logs)
# --------------------------------------------------------------------------
def _ts(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    from .ledger import parse_utc_us

    return parse_utc_us(value) / 1_000_000


def _percentile(values: List[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[idx]


def analyze_events(records: Sequence[Mapping[str, Any]], op_prefix: Optional[str] = None,
                   min_share: float = 0.1) -> Report:
    """Find bottlenecks in executed operations recorded as urtlog ``LOG`` records.

    * ``slow-operation``    – operation (``op_path``) taking a disproportionate share of busy time
    * ``tail-latency``      – p95 far above the median (unstable / contended operation)
    * ``failing-operation`` – high failure rate (retries waste capacity)
    * ``critical-operation`` – on the critical path of the URN lineage DAG
      (``URN_OUT`` of one run feeding ``URN_IN`` of another)
    * ``handoff-latency``   – long idle gap between a producer finishing and its consumer starting
    """
    runs, coverage = _collect_runs(records, op_prefix)
    report = Report()
    report.summary["duration_coverage"] = coverage
    if not runs:
        report.summary["events"] = {"runs": 0, "operations": 0}
        return report
    busy = sum(r["dur"] for r in runs) or 1e-12
    by_op: Dict[str, List[Dict[str, Any]]] = {}
    for r in runs:
        by_op.setdefault(r["op"], []).append(r)
    ops_summary: Dict[str, Dict[str, Any]] = {}
    for op, items in by_op.items():
        stats = _op_stats(items, busy)
        ops_summary[op] = stats
        report.extend(_operation_findings(op, stats, len(by_op), min_share))
    lineage_items, dag_summary, n_edges = _lineage_findings(runs, min_share)
    report.extend(lineage_items)
    top_ops = dict(sorted(ops_summary.items(), key=lambda kv: -kv[1]["total_s"])[:10])
    report.summary["events"] = {"runs": len(runs), "operations": len(by_op), "busy_seconds": round(busy, 6),
                                "lineage_edges": n_edges, **dag_summary, "top_operations": top_ops}
    return report


def _collect_runs(records: Sequence[Mapping[str, Any]], op_prefix: Optional[str]
                  ) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    runs = []
    coverage = {"parsed_records": len(records), "matching_logs": 0, "other_kinds": 0,
                "outside_prefix": 0, "missing_bounds": 0, "invalid_bounds": 0, "measured": 0}
    for rec in records:
        if rec.get("kind") != "log":
            coverage["other_kinds"] += 1
            continue
        uri = rec.get("uri")
        op = uri.get("op_path") if isinstance(uri, Mapping) else None
        op = op if isinstance(op, str) else "op/unknown"
        if op_prefix and not op.startswith(op_prefix):
            coverage["outside_prefix"] += 1
            continue
        coverage["matching_logs"] += 1
        urt = rec.get("urt")
        if not isinstance(urt, Mapping):
            coverage["invalid_bounds"] += 1
            continue
        first, last = urt.get("start"), urt.get("end")
        if first is None or last is None:
            coverage["missing_bounds"] += 1
            continue
        if not isinstance(first, str) or not isinstance(last, str):
            coverage["invalid_bounds"] += 1
            continue
        try:
            start, end = _ts(first), _ts(last)
        except (ValueError, OverflowError, OSError):
            start = end = None
        if start is None or end is None or end < start:
            coverage["invalid_bounds"] += 1
            continue
        runs.append({"id": rec.get("event_id"), "op": op, "start": start, "end": end, "dur": end - start,
                     "status": rec.get("status"), "inputs": [r.get("urn") for r in rec.get("inputs") or ()],
                     "outputs": [r.get("urn") for r in rec.get("outputs") or ()]})
    coverage["measured"] = len(runs)
    return runs, coverage


def _op_stats(items: List[Dict[str, Any]], busy: float) -> Dict[str, Any]:
    durs = [r["dur"] for r in items]
    total = sum(durs)
    return {"runs": len(items), "total_s": round(total, 6), "share": round(total / busy, 4),
            "p50_s": round(_percentile(durs, 0.5), 6), "p95_s": round(_percentile(durs, 0.95), 6),
            "failed": sum(1 for r in items if r["status"] == "failed")}


def _operation_findings(op: str, s: Dict[str, Any], n_ops: int, min_share: float) -> List[Bottleneck]:
    out = []
    share, runs, p50, p95 = s["share"], s["runs"], s["p50_s"], s["p95_s"]
    if n_ops > 1 and share >= max(min_share, 1.5 / n_ops):  # disproportionate vs. a fair share
        out.append(Bottleneck(
            "pipeline", "slow-operation", op,
            f"{share:.0%} of busy time ({s['total_s']:.3f}s over {runs} run(s), p95 {p95:.3f}s)",
            min(1.0, 0.2 + share), suggestion="optimise or cache this operation – it dominates execution time",
            metrics=s))
    if runs >= 5 and p50 > 0 and p95 >= 3 * p50:
        out.append(Bottleneck(
            "pipeline", "tail-latency", op, f"p95 {p95:.3f}s is {p95 / p50:.1f}x the median {p50:.3f}s",
            min(0.9, 0.3 + 0.05 * (p95 / p50)), suggestion="look for contention, cold caches or retries", metrics=s))
    rate = s["failed"] / runs if runs else 0.0
    if runs >= 3 and rate >= 0.2:
        out.append(Bottleneck(
            "pipeline", "failing-operation", op, f"{s['failed']}/{runs} runs failed ({rate:.0%})",
            min(1.0, 0.3 + 0.6 * rate), suggestion="fix the root cause – failed runs and retries burn capacity",
            metrics=s))
    return out


def _lineage_graph(runs: List[Dict[str, Any]]):
    """Producer→consumer edges where a run's ``URN_OUT`` is another run's ``URN_IN``."""
    producer: Dict[str, Dict[str, Any]] = {}
    for r in sorted(runs, key=lambda r: r["end"]):
        for urn in r["outputs"]:
            if urn and not urn.startswith("urn:bottlenecks:finding:"):
                producer.setdefault(urn, r)
    by_id = {r["id"]: r for r in runs}
    tasks, edges = [], []
    for r in runs:
        deps = sorted({producer[u]["id"] for u in r["inputs"] if u in producer and producer[u]["id"] != r["id"]})
        tasks.append({"name": r["id"], "duration": r["dur"], "deps": deps})
        edges.extend((by_id[d], r) for d in deps)
    return by_id, tasks, edges


def _lineage_findings(runs: List[Dict[str, Any]], min_share: float):
    by_id, tasks, edges = _lineage_graph(runs)
    if not edges:
        return [], {}, 0
    items: List[Bottleneck] = []
    summary: Dict[str, Any] = {}
    try:
        cp: Optional[Dict[str, Any]] = critical_path(tasks)
    except (CycleError, ValueError):
        cp = None
    if cp and cp["makespan"]:
        chain = [by_id[n] for n in cp["critical_path"]]
        summary = {"critical_path": [r["op"] for r in chain], "critical_seconds": round(cp["makespan"], 6),
                   "wall_seconds": round(max(r["end"] for r in chain) - min(r["start"] for r in chain), 6),
                   "parallelism": round(cp["parallelism"], 3)}
        for r in chain:
            share = r["dur"] / cp["makespan"]
            if share >= max(min_share, 0.2):
                items.append(Bottleneck(
                    "pipeline", "critical-operation", r["op"],
                    f"on the critical path of the lineage DAG, {share:.0%} of {cp['makespan']:.3f}s",
                    min(1.0, 0.25 + share), suggestion="speeding this up shortens the whole chain",
                    metrics={"event_id": r["id"], "duration_s": round(r["dur"], 6), "share": round(share, 4)}))
    for prod, cons in edges:
        gap = cons["start"] - prod["end"]
        if gap > max(1.0, 0.5 * cons["dur"]):
            items.append(Bottleneck(
                "pipeline", "handoff-latency", f"{prod['op']} -> {cons['op']}",
                f"consumer started {gap:.1f}s after its input was produced",
                min(0.8, 0.2 + 0.6 * gap / (gap + cons["dur"] + prod["dur"])),
                suggestion="trigger downstream work on completion events instead of polling/schedules",
                metrics={"gap_s": round(gap, 3), "producer": prod["id"], "consumer": cons["id"]}))
    return items, summary, len(edges)
