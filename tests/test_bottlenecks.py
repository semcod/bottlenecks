import json
import os
import time

import pytest

from bottlenecks import (
    Report, analyze_dag, analyze_metrics, analyze_pipeline, analyze_source, analyze_stages,
    critical_path, profile, profile_callable, render,
)
from bottlenecks.cli import run
from bottlenecks.pipeline import CycleError

SLOW = '''
import re, time, requests
def f(items, other):
    out = ""
    for a in items:
        for b in other:
            if a == b:
                out += "x"
        time.sleep(0)
        requests.get("http://x")
        re.match("a+", a)
        sorted(other)
    return out
'''


def kinds(items):
    return {b.kind for b in items}


def test_code_rules():
    found = kinds(analyze_source(SLOW, "slow.py"))
    assert {"nested-loops", "io-in-loop", "str-concat-in-loop", "regex-in-loop", "sort-in-loop"} <= found


def test_code_clean():
    assert analyze_source("def f(x):\n    return x + 1\n") == []


def test_complexity():
    body = "def f(x):\n" + "".join(f"    if x == {i}:\n        return {i}\n" for i in range(15))
    assert "high-complexity" in kinds(analyze_source(body, complexity_threshold=10))


def busy():
    return sum(i * i for i in range(200_000))


def test_runtime_profile_callable():
    result, report = profile_callable(busy, threshold=0.01)
    assert result > 0
    assert report.summary["runtime"]["total_seconds"] > 0
    assert report.items, "expected at least one hot function"


def test_runtime_context_manager():
    with profile(threshold=0.01) as p:
        busy()
    assert isinstance(p.report, Report)


TASKS = [
    {"name": "fetch", "duration": 2},
    {"name": "build", "duration": 10, "deps": ["fetch"]},
    {"name": "lint", "duration": 1, "deps": ["fetch"]},
    {"name": "docs", "duration": 1, "deps": ["fetch"]},
    {"name": "test", "duration": 5, "deps": ["build"]},
    {"name": "publish", "duration": 1, "deps": ["test", "lint", "docs"]},
]


def test_critical_path():
    cp = critical_path(TASKS)
    assert cp["critical_path"] == ["fetch", "build", "test", "publish"]
    assert cp["makespan"] == 18
    assert cp["slack"]["lint"] == 14


def test_dag_report():
    report = analyze_dag(TASKS)
    assert ("critical-task", "build") in {(b.kind, b.location) for b in report.items}
    assert ("choke-point", "fetch") in {(b.kind, b.location) for b in report.items}


def test_cycle():
    with pytest.raises(CycleError):
        critical_path([{"name": "a", "deps": ["b"]}, {"name": "b", "deps": ["a"]}])


def test_stages():
    report = analyze_stages([
        {"name": "parse", "service_time": 0.1},
        {"name": "embed", "service_time": 1.0, "workers": 2},
        {"name": "store", "service_time": 0.2},
    ], arrival_rate=2.5)
    assert report.summary["stages"]["limiting_stage"] == "embed"
    top = report.ranked()[0]
    assert top.location == "embed" and top.severity == "critical"


def test_system_metrics():
    report = analyze_metrics({"cpu_count": 4, "cpu_percent": 99, "load_avg": (12, 8, 4), "iowait_percent": 40,
                              "mem_percent": 97, "swap_percent": 80, "disk_percent": {"/": 99}})
    assert {"cpu-saturation", "run-queue", "io-wait", "memory-pressure", "swapping", "disk-full"} <= kinds(report.items)
    assert not analyze_metrics({"cpu_count": 4, "cpu_percent": 10, "load_avg": (0.5, 0.5, 0.5),
                                "mem_percent": 30, "disk_percent": {"/": 40}}).items


def test_render_formats():
    report = analyze_pipeline({"tasks": TASKS})
    assert json.loads(render(report, "json"))["count"] == len(report.items)
    assert "| Severity |" in render(report, "markdown")
    assert "bottlenecks:" in render(report, "text")


def test_cli(tmp_path, capsys):
    src = tmp_path / "slow.py"
    src.write_text(SLOW)
    spec = tmp_path / "p.json"
    spec.write_text(json.dumps({"tasks": TASKS}))
    assert run(["code", str(tmp_path), "-f", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["count"] >= 5
    assert run(["code", str(src), "--fail-on", "medium"]) == 1
    assert run(["pipeline", str(spec)]) == 0
    assert run(["system", "--interval", "0.05", "-f", "json"]) == 0
