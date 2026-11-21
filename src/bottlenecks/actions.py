"""Default action catalog: every capability of the library as a registry entry.

Handlers are plain functions over the analyzers; the engine passes itself for
actions that need the ledger (history / plan).
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from .code import analyze_code
from .ledger import LedgerNotConfigured, findings_to_report, load_events, now_us, parse_duration_seconds, parse_utc_us
from .models import Report
from .pipeline import analyze_events, analyze_pipeline
from .registry import Action, ActionRegistry, Param
from .runtime import load_pstats, profile_script
from .system import analyze_system

if TYPE_CHECKING:  # pragma: no cover
    from .engine import Engine

SEVERITY_ENUM = ("info", "low", "medium", "high", "critical")


def _require_ledger(engine: "Engine"):
    if engine.ledger is None:
        raise LedgerNotConfigured("history/plan actions need a ledger: pass --state DIR or set BOTTLENECKS_STATE_DIR")
    return engine.ledger


def build_registry(engine: "Engine") -> ActionRegistry:
    reg = ActionRegistry()

    # ---------------------------------------------------------------- analyses
    def code_scan(path: List[str], complexity: int, max_lines: int) -> Report:
        return analyze_code(path, complexity_threshold=complexity, length_threshold=max_lines)

    def runtime_profile(script: str, args: Optional[List[str]] = None, threshold: float = 0.05) -> Report:
        if not os.path.isfile(script):
            raise FileNotFoundError(script)
        return profile_script(script, args or [], threshold=threshold)

    def runtime_pstats(file: str, threshold: float) -> Report:
        return load_pstats(file, threshold=threshold)

    def pipeline_analyze(spec: str) -> Report:
        text = spec if spec.lstrip().startswith("{") else open(spec, encoding="utf-8").read()
        return analyze_pipeline(json.loads(text))

    def pipeline_urtlog(source: str, op_prefix: Optional[str] = None, min_share: float = 0.1) -> Report:
        return analyze_events(load_events(source), op_prefix=op_prefix, min_share=min_share)

    def system_sample(interval: float, mounts: List[str]) -> Report:
        return analyze_system(interval=interval, mounts=mounts)

    def scan_all(path: List[str], interval: float) -> Report:
        return analyze_code(path).merge(analyze_system(interval=interval))

    sev = Param("min_severity", "string", enum=SEVERITY_ENUM, description="hide findings below this severity")
    top = Param("top", "integer", description="return at most N findings")
    reg.register(Action("code", "scan", "Static bottleneck scan of Python code", code_scan, (
        Param("path", "array", default=["."], description="files/directories to scan"),
        Param("complexity", "integer", default=10, description="cyclomatic complexity threshold"),
        Param("max_lines", "integer", default=80, description="function length threshold"), sev, top),
        effects=("read",), menu="Code", nl_intents={"pl": ["znajdź wąskie gardła w kodzie src"],
                                                    "en": ["find bottlenecks in code src"]}))
    reg.register(Action("runtime", "profile", "Profile a Python script with cProfile (executes it)", runtime_profile, (
        Param("script", "string", required=True, description="path to the script"),
        Param("args", "array", default=None, description="script arguments"),
        Param("threshold", "number", default=0.05, description="min share of total time"), sev, top),
        effects=("process",), menu="Runtime", nl_intents={"pl": ["sprofiluj skrypt app.py"], "en": ["profile app.py"]}))
    reg.register(Action("runtime", "pstats", "Analyse a saved cProfile .prof file", runtime_pstats, (
        Param("file", "string", required=True), Param("threshold", "number", default=0.05), sev, top),
        effects=("read",), menu="Runtime"))
    reg.register(Action("pipeline", "analyze", "Critical path / choke points / saturation of a pipeline spec",
                        pipeline_analyze, (Param("spec", "string", required=True,
                                                 description="JSON file with tasks and/or stages (or inline JSON)"),
                                           sev, top), effects=("read",), menu="Pipeline"))
    reg.register(Action("pipeline", "urtlog", "Bottlenecks in recorded executions (urtlog events.jsonl)",
                        pipeline_urtlog, (Param("source", "string", required=True,
                                                description="urtlog/bottlenecks state dir or events.jsonl"),
                                          Param("op_prefix", "string", description="restrict to op_path prefix"),
                                          Param("min_share", "number", default=0.1), sev, top),
                        effects=("read",), menu="Pipeline"))
    reg.register(Action("system", "sample", "Sample host CPU / load / I/O wait / memory / swap / disk", system_sample, (
        Param("interval", "number", default=0.5), Param("mounts", "array", default=["/"]), sev, top),
        effects=("read",), menu="System"))
    reg.register(Action("scan", "all", "Code scan + system sample in one report", scan_all, (
        Param("path", "array", default=["."]), Param("interval", "number", default=0.5), sev, top),
        effects=("read",), menu="Scan"))

    # ---------------------------------------------------------------- history
    def history_list(limit: int, action: Optional[str] = None) -> Dict[str, Any]:
        return {"runs": _require_ledger(engine).runs(action_id=action, limit=limit)}

    def history_diff(base: Optional[str] = None, head: Optional[str] = None, action: Optional[str] = None) -> Report:
        d = _require_ledger(engine).diff(base=base, head=head, action_id=action)
        report = findings_to_report(d["new"] + d["worsened"])
        report.summary["diff"] = {k: (len(v) if isinstance(v, list) else v) for k, v in d.items()}
        report.summary["diff"]["resolved_items"] = [{"location": f["location"], "kind": f["kind"]}
                                                    for f in d["resolved"][:20]]
        return report

    def history_trend(limit: int, action: Optional[str] = None) -> Dict[str, Any]:
        return {"trend": _require_ledger(engine).trend(action_id=action, limit=limit)}

    def history_lineage(urn: str) -> Dict[str, Any]:
        return _require_ledger(engine).lineage(urn)

    reg.register(Action("history", "list", "Recorded runs (urtlog LOG records)", history_list, (
        Param("limit", "integer", default=20), Param("action", "string")), effects=("read",),
        produces_report=False, menu="History"))
    reg.register(Action("history", "diff", "New / worsened / resolved bottlenecks between two runs", history_diff, (
        Param("base", "string", description="run id (default: previous run with same input)"),
        Param("head", "string", description="run id (default: latest run)"),
        Param("action", "string"), sev, top), effects=("read",), menu="History"))
    reg.register(Action("history", "trend", "Findings count / score over time", history_trend, (
        Param("limit", "integer", default=20), Param("action", "string")), effects=("read",),
        produces_report=False, menu="History"))
    reg.register(Action("history", "lineage", "Which run produced / consumed a URN", history_lineage, (
        Param("urn", "string", required=True),), effects=("read",), produces_report=False, menu="History"))

    # ---------------------------------------------------------------- plans
    def plan_schedule(command: str, every: Optional[str] = None, at: Optional[str] = None,
                      window: Optional[str] = None, allow_process: bool = False) -> Dict[str, Any]:
        ledger = _require_ledger(engine)
        cmd, action, bound = engine.compile(command)
        if action.entity in ("plan", "history", "registry"):
            raise ValueError(f"'{action.id}' cannot be scheduled")
        if "process" in action.effects and not allow_process:
            raise PermissionError(f"'{action.id}' executes code; schedule it with allow_process=true")
        canonical = cmd.to_canonical_string()
        record = ledger.schedule(action_id=action.id, base_uri=action.uri, arguments=bound, canonical=canonical,
                                 at_us=parse_utc_us(at) if at else now_us(),
                                 step_seconds=parse_duration_seconds(every) if every else 0,
                                 window_seconds=parse_duration_seconds(window) if window else 0)
        return {"plan": record["event_id"], "dsl": record["dsl"], "at": record["urt"]["at"],
                "step_seconds": record["urt"]["step_seconds"]}

    def plan_run_due(now: Optional[str] = None) -> Dict[str, Any]:
        return engine.run_due(parse_utc_us(now) if now else None)

    def plan_list() -> Dict[str, Any]:
        plans = _require_ledger(engine).pending_plans()
        return {"plans": [{"plan": p["event_id"], "at": p["urt"]["at"], "step_seconds": p["urt"]["step_seconds"],
                           "dsl": (p.get("metadata") or {}).get("canonical_dsl"), "record": p["dsl"]} for p in plans]}

    reg.register(Action("plan", "schedule", "Schedule a (recurring) DSL command as an urtlog PLAN record",
                        plan_schedule, (Param("command", "string", required=True, description="canonical DSL"),
                                        Param("every", "string", description="PT300S / 5m / 1h"),
                                        Param("at", "string", description="RFC3339 first run (default now)"),
                                        Param("window", "string", description="misfire window"),
                                        Param("allow_process", "boolean", default=False)),
                        effects=("write",), produces_report=False, menu="Plans"))
    reg.register(Action("plan", "run_due", "Execute due PLAN records (causal LOG records, idempotent)", plan_run_due,
                        (Param("now", "string", description="RFC3339 override for tests"),), effects=("write",),
                        produces_report=False, menu="Plans"))
    reg.register(Action("plan", "list", "Pending PLAN records", plan_list, (), effects=("read",),
                        produces_report=False, menu="Plans"))

    # ---------------------------------------------------------------- registry
    reg.register(Action("registry", "list", "Action registry (nl-api-llm manifest)", lambda: reg.manifest(), (),
                        effects=("pure",), produces_report=False, menu="Help"))
    return reg
