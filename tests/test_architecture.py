import io
import json
import os
import sys
import threading
import types
import urllib.error
import urllib.request

import pytest

from bottlenecks import DSLCommand, Engine, Ledger, analyze_events, load_events
from bottlenecks.cli import run
from bottlenecks.interfaces import make_server, mcp_handle, serve_mcp
from bottlenecks.ledger import build_record, finding_fingerprint, format_utc_us, now_us, parse_duration_seconds
from bottlenecks.models import Bottleneck
from bottlenecks.nl import LLMTranslator

SLOW = '''
import time
def slow(items, other):
    out = ""
    for a in items:
        for b in other:
            out += "x"
        time.sleep(0)
    return out
'''


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in ("BOTTLENECKS_STATE_DIR", "URTLOG_STATE_DIR", "BOTTLENECKS_LLM_URL", "BOTTLENECKS_LLM_MODEL"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "slow.py").write_text(SLOW)
    (tmp_path / "pipeline.json").write_text(json.dumps({"tasks": [
        {"name": "fetch", "duration": 2}, {"name": "build", "duration": 10, "deps": ["fetch"]},
        {"name": "test", "duration": 5, "deps": ["build"]}]}))
    (tmp_path / "app.py").write_text("print(sum(i * i for i in range(1000)))\n")
    return tmp_path


@pytest.fixture
def engine(project):
    eng = Engine(ledger=Ledger(project / "state", bridge=None))  # LLM layer disabled: env is cleaned above
    yield eng
    eng.close()


# ------------------------------------------------------------------- DSL
def test_dsl_roundtrip_and_errors():
    cmd = DSLCommand.from_text('code.scan path=src top=5 filter:severity=high note="a b"')
    assert cmd.action_id == "code.scan" and cmd.filters == {"severity": "high"}
    again = DSLCommand.from_text(cmd.to_canonical_string())
    assert again.arguments == cmd.arguments and again.filters == cmd.filters
    for bad in ("", "scan", "code.scan novalue", "Code.Sc@n x=1"):
        with pytest.raises(ValueError):
            DSLCommand.from_text(bad)
    assert DSLCommand.from_dict({"entity": "system", "operation": "sample"}).action_id == "system.sample"


def test_registry_manifest(engine):
    m = engine.registry.manifest()
    assert m["schema"].startswith("wellmanifest.nl-api-llm")
    ids = {e["id"] for e in m["endpoints"]}
    assert {"code.scan", "runtime.profile", "pipeline.urtlog", "system.sample", "history.diff", "plan.run_due"} <= ids
    profile = next(e for e in m["endpoints"] if e["id"] == "runtime.profile")
    assert profile["actionUri"] == "proc://bottlenecks/runtime/profile/v1" and profile["requiresConfirmation"]
    assert not profile["readOnly"]


# ---------------------------------------------------------------- engine
def test_engine_validation_and_presentation(engine, project):
    assert engine.execute("code.scan bogus=1").status == "VALIDATION_ERROR"
    assert engine.execute("code.scann").status == "NOT_FOUND"
    assert "did you mean" in engine.execute("code.scann").errors[0]["message"]
    assert engine.execute("code.scan filter:color=red").status == "VALIDATION_ERROR"
    assert engine.execute("code.scan complexity=abc").status == "VALIDATION_ERROR"
    full = engine.execute("code.scan path=src")
    top1 = engine.execute("code.scan path=src top=1")
    only_kind = engine.execute("code.scan path=src filter:kind=str-concat-in-loop")
    assert full.data["count"] >= 3 and top1.data["shown"] == 1
    assert {b["kind"] for b in only_kind.data["items"]} == {"str-concat-in-loop"}
    assert full.meta["actionUri"] == "proc://bottlenecks/code/scan/v1" and full.meta["urtlog"].startswith("LOG URT(")


def test_engine_failure_is_recorded(engine):
    res = engine.execute("pipeline.analyze spec=missing.json")
    assert res.status == "EXECUTION_ERROR"
    runs = engine.ledger.runs()
    assert runs[0]["status"] == "failed"


# -------------------------------------------------------------------- NL
@pytest.mark.parametrize("query, action, expect", [
    ("pokaż 5 najgorszych wąskich gardeł w kodzie src", "code.scan", {"top": 5, "path": ["src"]}),
    ("find high severity bottlenecks in code src", "code.scan", {"min_severity": "high"}),
    ("sprawdź obciążenie systemu przez 0.05 s", "system.sample", {"interval": 0.05}),
    ("is the host overloaded for 0.05 sec?", "system.sample", {}),
    ("sprawdź procesr przez 0.05 s", "system.sample", {}),
    ("pokaż ścieżkę krytyczną pipeline.json", "pipeline.analyze", {"spec": "pipeline.json"}),
    ("porównaj z poprzednim skanem", "history.diff", {}),
    ("co potrafisz?", "registry.list", {}),
    ("src", "code.scan", {"path": ["src"]}),
])
def test_nl_fast_path(engine, query, action, expect):
    cmd = engine.parser.parse(query)
    assert cmd is not None and cmd.action_id == action, engine.parser.last_trace
    for k, v in expect.items():
        assert cmd.arguments.get(k) == v
    if action == "pipeline.analyze":
        assert "min_severity" not in cmd.arguments  # "ścieżka krytyczna" is not a severity


def test_nl_schedule_and_confirmation(engine):
    cmd = engine.parser.parse("skanuj kod src co 5 minut")
    assert cmd.action_id == "plan.schedule" and cmd.arguments["every"] == "PT300S"
    assert cmd.arguments["command"].startswith("code.scan")
    res = engine.ask("sprofiluj skrypt app.py")
    assert res.status == "UNAUTHORIZED" and res.meta["actionId"] == "runtime.profile"
    assert engine.ask("sprofiluj skrypt app.py", confirmed=True).success


def test_nl_guards(engine):
    neg = engine.ask("nie skanuj kodu src")
    assert neg.status == "CLARIFY" and neg.errors[0]["code"] == "NL_NEGATION_GUARD"
    unknown = engine.ask("xyzzy frobnicate")
    assert unknown.status == "CLARIFY" and unknown.errors[0]["code"] == "NL_UNRECOGNIZED_INTENT"
    direct = engine.ask("system.sample interval=0.05")
    assert direct.success and direct.meta["sourceLayer"] == "direct_dsl"


def test_llm_fallback_learns_into_semantic_cache(project):
    calls = []

    def fake(messages, schema):
        calls.append(messages[-1]["content"])
        assert "system.sample" in schema["properties"]["action"]["enum"]
        return '```json\n{"status": "ok", "action": "system.sample", "arguments": {"interval": 0.05}}\n```'

    eng = Engine(ledger=Ledger(project / "state", bridge=None))
    eng.translator = LLMTranslator(eng.registry, complete=fake)
    first = eng.ask("how is the box doing today")
    assert first.success and first.meta["sourceLayer"] == "llm_fallback"
    second = eng.ask("how is the box doing today")
    assert second.success and second.meta["sourceLayer"] == "semantic_cache" and len(calls) == 1
    assert eng.ask("how is the box not doing today").errors[0]["code"] == "NL_NEGATION_GUARD"
    assert (project / "state" / "nl-cache.json").exists()
    eng.close()


def test_llm_fail_closed(project):
    eng = Engine()
    eng.translator = LLMTranslator(eng.registry, complete=lambda m, s: '{"status":"ok","action":"os.system"}')
    res = eng.ask("obliterate the universe quickly")
    assert res.status == "CLARIFY" and "unknown action" in res.errors[0]["message"]
    eng.translator = LLMTranslator(eng.registry, complete=lambda m, s: '{"status":"clarify","question":"Which?",'
                                                                     '"options":["code.scan path=src"]}')
    res = eng.ask("hmm something odd here")
    assert res.status == "CLARIFY" and res.data["clarify"]["options"] == ["code.scan path=src"]


def test_option_network(engine, project):
    s = engine.suggest("code", context={"source_dirs": ["src"]})
    first = s["suggestions"][0]
    assert first["action_id"] == "code.scan" and first["next_options"][0]["completion_text"] == "code.scan path=src"
    prof = engine.suggest("runtime.prof")["suggestions"][0]
    assert prof["requires_confirmation"] and prof["badge"] == "EXEC"


# ---------------------------------------------------------------- ledger
def test_ledger_records_are_urtlog_v2(engine, project):
    engine.execute("code.scan path=src")
    recs = load_events(project / "state")
    rec = recs[-1]
    assert rec["schema"] == "uritime.urtlog.record/v2" and rec["source"] == "bottlenecks"
    assert rec["uri"]["op_path"] == "op/proc/bottlenecks/code/scan/v1"
    assert rec["urt"]["kind"] == "interval" and rec["urt"]["time_path"].startswith("t/")
    assert rec["inputs"][0]["urn"].startswith("urn:bottlenecks:input:")
    assert rec["inputs"][0]["digest"].startswith("ni:///sha-256;")
    assert any(o["urn"].startswith("urn:bottlenecks:finding:") for o in rec["outputs"])
    assert rec["dsl"].startswith("LOG URT(at=") and "STATUS(succeeded)" in rec["dsl"]
    for forbidden in (" ", ",", "(", ")"):
        assert forbidden not in rec["uri"]["raw"]


def test_ledger_rebuild_idempotence_lineage(engine, project):
    res = engine.execute("code.scan path=src")
    ledger = engine.ledger
    rec = ledger.get(res.meta["eventId"])
    assert ledger.append(rec) is False  # same event id -> ignored
    report_urn = rec["outputs"][0]["urn"]
    assert ledger.lineage(report_urn)["produced_by"][0]["event_id"] == rec["event_id"]
    engine.close()
    os.remove(project / "state" / "ledger.sqlite3")
    reopened = Ledger(project / "state", bridge=None)
    assert reopened.runs()[0]["event_id"] == rec["event_id"] and reopened.findings(rec["run_id"])
    reopened.close()


def test_regression_diff_and_trend(engine, project):
    engine.execute("code.scan path=src")
    (project / "src" / "worse.py").write_text("import re\ndef f(xs):\n    for x in xs:\n        re.match('a+', x)\n")
    engine.execute("code.scan path=src")
    diff = engine.execute("history.diff action=code.scan")
    assert diff.success and {b["kind"] for b in diff.data["items"]} == {"regex-in-loop"}
    assert diff.data["summary"]["diff"]["new"] == 1
    trend = engine.execute("history.trend action=code.scan").data["trend"]
    assert len(trend) == 2 and trend[1]["findings"] > trend[0]["findings"]


def test_fingerprint_ignores_line_numbers():
    a = Bottleneck("code", "nested-loops", "src/a.py:10 (f)", "m", 0.5)
    b = Bottleneck("code", "nested-loops", "src/a.py:42 (f)", "m", 0.5)
    assert finding_fingerprint(a) == finding_fingerprint(b)


def test_history_requires_ledger(project):
    eng = Engine()
    res = eng.execute("history.list")
    assert res.status == "VALIDATION_ERROR" and res.errors[0]["code"] == "LEDGER_NOT_CONFIGURED"


# ------------------------------------------------------------------ plans
def test_plans_run_due_misfire_and_recurrence(engine):
    t0 = now_us()
    ok = engine.execute(f"plan.schedule command=\"code.scan path=src\" every=5m at={format_utc_us(t0)}")
    assert ok.success
    assert engine.run_due(t0 - 1_000_000)["waiting"]
    done = engine.run_due(t0 + 1_000_000)
    assert len(done["executed"]) == 1 and done["executed"][0]["status"] == "OK"
    assert engine.run_due(t0 + 2_000_000)["executed"] == []  # idempotent
    late = engine.run_due(t0 + 3600 * 1_000_000)  # next occurrence missed its window
    assert late["canceled"] and late["scheduled_next"]
    runs = engine.ledger.runs(action_id="code.scan")
    assert any(r["cause"] == ok.data["plan"] and r["status"] == "succeeded" for r in runs)
    assert runs[0]["status"] == "canceled"  # the misfired second occurrence is settled, not silently dropped


def test_plan_guards(engine):
    assert engine.execute('plan.schedule command="runtime.profile script=app.py"').status == "UNAUTHORIZED"
    assert engine.execute('plan.schedule command="plan.list"').status == "EXECUTION_ERROR"
    assert engine.execute('plan.schedule command="runtime.profile script=app.py" allow_process=true').success
    assert parse_duration_seconds("PT1H") == 3600 and parse_duration_seconds("5m") == 300


# --------------------------------------------------------------- urtlog events
def _rec(op, start, end, ins=(), outs=(), status="succeeded"):
    ref = lambda u: {"urn": u, "res_path": "res/x", "role": "primary", "digest": None,  # noqa: E731
                     "sensitivity": "internal", "planned": False}
    return build_record(kind="log", uri=f"proc://svc/{op}/v1", at_us=start, start_us=start, end_us=end,
                        inputs=[ref(u) for u in ins], outputs=[ref(u) for u in outs], status=status, run_id=op + str(start))


def test_analyze_events_lineage_and_ops():
    s = 1_700_000_000_000_000
    sec = 1_000_000
    records = [
        _rec("extract", s, s + 10 * sec, outs=["urn:x:raw"]),
        _rec("transform", s + 15 * sec, s + 16 * sec, ins=["urn:x:raw"], outs=["urn:x:clean"]),
        _rec("load", s + 16 * sec, s + 17 * sec, ins=["urn:x:clean"]),
    ] + [_rec("flaky", s + i * sec, s + i * sec + sec // 10, status="failed" if i % 2 else "succeeded")
         for i in range(4)] + [_rec("jittery", s + i * sec, s + i * sec + (8 * sec if i == 5 else sec // 10))
                               for i in range(6)]
    report = analyze_events(records)
    kinds = {(b.kind, b.location) for b in report.items}
    assert ("critical-operation", "op/proc/svc/extract/v1") in kinds
    assert ("handoff-latency", "op/proc/svc/extract/v1 -> op/proc/svc/transform/v1") in kinds
    assert ("failing-operation", "op/proc/svc/flaky/v1") in kinds
    assert ("tail-latency", "op/proc/svc/jittery/v1") in kinds
    assert report.summary["events"]["critical_path"][0] == "op/proc/svc/extract/v1"


# ----------------------------------------------------------- urtlog bridge
def test_urtlog_bridge_mirrors_records(project, monkeypatch):
    appended = []
    store_mod = types.ModuleType("urtlog.store")

    class UrtLogStore:
        def __init__(self, state_dir):
            self.state_dir = state_dir

        def append(self, rec):
            appended.append(rec)
            return True

        def close(self):
            pass

    store_mod.UrtLogStore = UrtLogStore
    store_mod._record_from_json = lambda raw: json.loads(raw)
    pkg = types.ModuleType("urtlog")
    pkg.store = store_mod
    monkeypatch.setitem(sys.modules, "urtlog", pkg)
    monkeypatch.setitem(sys.modules, "urtlog.store", store_mod)
    monkeypatch.setenv("URTLOG_STATE_DIR", str(project / "urt"))
    eng = Engine(ledger=Ledger(project / "state"))
    eng.execute("code.scan path=src")
    assert appended and appended[0]["schema"] == "uritime.urtlog.record/v2"
    eng.close()


# ------------------------------------------------------------- interfaces
def test_rest_parity(engine):
    server = make_server(engine, port=0)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def call(path, body=None):
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    try:
        assert call("/health")[1]["status"] == "ok"
        assert "urtlog-record" in call("/schema")[1]["schemas"]
        assert call("/suggest?q=system")[1]["suggestions"][0]["action_id"] == "system.sample"
        code, body = call("/dsl", {"dsl": "code.scan path=src top=2"})
        assert code == 200 and body["data"]["shown"] == 2
        code, body = call("/dsl", {"entity": "code", "operation": "scan", "arguments": {"path": ["src"]}})
        assert code == 200 and body["success"]
        code, body = call("/query", {"query": "pokaż wąskie gardła w kodzie src"})
        assert code == 200 and body["meta"]["sourceLayer"] == "nl_fast_path"
        assert call("/dsl", {"dsl": "runtime.profile script=app.py"})[0] == 403
        assert call("/dsl", {"dsl": "runtime.profile script=app.py", "confirm": True})[0] == 200
        assert call("/query", {"query": "nie rób nic z kodem"})[0] == 422
        assert call("/nope")[0] == 404
        assert call("/history")[1]["data"]["runs"]
    finally:
        server.shutdown()
        server.server_close()


def test_mcp_parity(engine):
    init = mcp_handle(engine, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert init["result"]["serverInfo"]["name"] == "bottlenecks"
    tools = {t["name"] for t in mcp_handle(engine, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]}
    assert tools == {"nl_ask", "execute_dsl", "suggest_options", "list_actions"}
    out = mcp_handle(engine, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                              "params": {"name": "nl_ask", "arguments": {"query": "find bottlenecks in code src"}}})
    payload = json.loads(out["result"]["content"][0]["text"])
    assert payload["success"] and not out["result"]["isError"]
    assert mcp_handle(engine, {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert mcp_handle(engine, {"jsonrpc": "2.0", "id": 4, "method": "nope"})["error"]["code"] == -32601
    bad = mcp_handle(engine, {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "execute_dsl",
                                                                                        "arguments": {}}})
    assert bad["error"]["code"] == -32602
    stdin = io.StringIO('{"jsonrpc":"2.0","id":9,"method":"ping"}\nnot json\n')
    stdout = io.StringIO()
    serve_mcp(engine, stdin, stdout)
    lines = [json.loads(x) for x in stdout.getvalue().splitlines()]
    assert lines[0]["result"] == {} and lines[1]["error"]["code"] == -32700


def test_cli_new_commands(project, capsys):
    state = str(project / "state")
    assert run(["--state", state, "dsl", "code.scan", "path=src", "-f", "json"]) == 0
    env = json.loads(capsys.readouterr().out)
    assert env["meta"]["canonicalDsl"].startswith("code.scan") and env["meta"]["runId"]
    assert run(["--state", state, "ask", "pokaż", "wąskie", "gardła", "w", "kodzie", "src", "--fail-on", "medium"]) == 1
    capsys.readouterr()
    assert run(["history"]) == 2
    assert run(["--state", state, "history", "-f", "json"]) == 0
    assert run(["--state", state, "plan", "schedule", "code.scan", "path=src", "--every", "5m"]) == 0
    assert run(["--state", state, "plan", "run-due"]) == 0
    assert '"executed"' in capsys.readouterr().out
    assert run(["registry"]) == 0 and "proc://bottlenecks/code/scan/v1" in capsys.readouterr().out
    assert run(["suggest", "sys"]) == 0
    assert run(["ask", "xyzzy"]) == 2
    assert run(["urtlog", state, "-f", "json"]) == 0


def test_cli_self_test(capsys):
    assert run(["self-test"]) == 0
    assert "12/12 checks passed" in capsys.readouterr().out
