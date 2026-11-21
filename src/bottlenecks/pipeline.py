"""Pipeline / DAG / queue bottleneck detection.

Two models are supported:

* **DAG of tasks** – ``[{"name": "build", "duration": 12, "deps": ["fetch"]}, ...]``.
  Computes the critical path, slack, parallelism and fan-in/fan-out choke points.
* **Stage pipeline** – ``[{"name": "parse", "service_time": 0.2, "workers": 2}, ...]``
  with an ``arrival_rate`` (items / second). Computes utilisation (Little / M/M/c
  style ``rho``) and the throughput-limiting stage.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence

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
                min(1.0, 0.15 + 0.5 * ratio), suggestion="make it fail fast, isolate failures and start independent branches early",
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
