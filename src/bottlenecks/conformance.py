"""Conformance self-test (``nl-dsl-llm`` CONF-07): ``bottlenecks self-test``.

Runs in a temporary ledger and checks every layer and interface end-to-end.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import textwrap
from typing import Callable, List, Tuple

_SAMPLE = textwrap.dedent('''
    import time
    def slow(items, other):
        for a in items:
            for b in other:
                time.sleep(0)
''')


def _checks(tmp: str) -> List[Tuple[str, Callable[[], bool]]]:
    from .engine import Engine
    from .interfaces import mcp_handle, schema_document
    from .ledger import Ledger, load_events

    src = os.path.join(tmp, "src")
    os.makedirs(src)
    with open(os.path.join(src, "slow.py"), "w", encoding="utf-8") as fh:
        fh.write(_SAMPLE)
    engine = Engine(ledger=Ledger(os.path.join(tmp, "state"), bridge=None))
    state: dict = {}

    def direct_dsl() -> bool:
        r = engine.execute(f"code.scan path={src} top=5")
        state["run1"] = r.meta.get("runId")
        return r.success and r.meta["sourceLayer"] == "direct_dsl" and r.data["count"] >= 2

    def pl_fast_path() -> bool:
        r = engine.ask(f"pokaż krytyczne wąskie gardła w kodzie {src}")
        return (r.success and r.meta["sourceLayer"] == "nl_fast_path"
                and r.meta["canonicalDsl"].startswith("code.scan") and "min_severity=critical" in r.meta["canonicalDsl"])

    def en_fast_path() -> bool:
        r = engine.ask("check system bottlenecks for 0.05 s")
        return r.success and r.meta["actionId"] == "system.sample"

    def negation_guard() -> bool:
        r = engine.ask(f"nie skanuj kodu {src}")
        return (not r.success) and r.errors[0]["code"] == "NL_NEGATION_GUARD"

    def validation() -> bool:
        a = engine.execute("code.scan bogus=1")
        b = engine.execute("code.explode")
        return a.status == "VALIDATION_ERROR" and b.status == "NOT_FOUND"

    def confirmation() -> bool:
        r = engine.execute("runtime.profile script=x.py", confirmed=False)
        return r.status == "UNAUTHORIZED" and r.errors[0]["code"] == "CONFIRMATION_REQUIRED"

    def option_network() -> bool:
        s = engine.suggest("code", context={"source_dirs": [src]})
        first = s["suggestions"][0] if s["suggestions"] else {}
        return first.get("action_id") == "code.scan" and s["latency_ms"] < 50 and bool(first.get("next_options"))

    def mcp_parity() -> bool:
        resp = mcp_handle(engine, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                   "params": {"name": "execute_dsl", "arguments": {"dsl_command": f"code.scan path={src}"}}})
        payload = json.loads(resp["result"]["content"][0]["text"])
        return payload["success"] and payload["meta"]["canonicalDsl"].startswith("code.scan")

    def schema_doc() -> bool:
        doc = schema_document(engine)
        return {"dsl-command", "dsl-result", "nl-query", "urtlog-record"} <= set(doc["schemas"]) and \
            len(doc["registry"]["endpoints"]) == len(engine.registry)

    def urtlog_records() -> bool:
        recs = load_events(os.path.join(tmp, "state"))
        ok = bool(recs) and all(r["schema"] == "uritime.urtlog.record/v2" and r["dsl"].startswith("LOG URT(")
                                for r in recs)
        try:  # cross-check with the real urtlog parser when installed
            from urtlog.dsl import parse_dsl_line  # type: ignore

            ok = ok and all(parse_dsl_line(r["dsl"]).uri.raw == r["uri"]["raw"] for r in recs)
        except ImportError:
            pass
        return ok

    def regression_diff() -> bool:
        with open(os.path.join(src, "slow.py"), "a", encoding="utf-8") as fh:
            fh.write("\ndef worse(x):\n    out = ''\n    for a in x:\n        out += 'y'\n    return out\n")
        engine.execute(f"code.scan path={src} top=5")
        r = engine.execute("history.diff action=code.scan")
        return r.success and any(b["kind"] == "str-concat-in-loop" for b in r.data["items"])

    def plans() -> bool:
        s = engine.execute(f"plan.schedule command=\"code.scan path={src}\" every=PT300S")
        due = engine.run_due()
        again = engine.run_due()
        return s.success and len(due["executed"]) == 1 and len(due["scheduled_next"]) == 1 and not again["executed"]

    return [
        ("Layer 2 direct DSL execution", direct_dsl),
        ("Layer 1 Polish fast path (+severity slot)", pl_fast_path),
        ("Layer 1 English fast path", en_fast_path),
        ("Polarity guard rejects negated intent", negation_guard),
        ("Registry validation (unknown param/action)", validation),
        ("Confirmation required for process effects", confirmation),
        ("Layer 0.5 option network bound to context", option_network),
        ("MCP tools/call parity", mcp_parity),
        ("Schema + nl-api-llm registry document", schema_doc),
        ("Ledger writes urtlog record/v2 LOG lines", urtlog_records),
        ("Regression diff between runs", regression_diff),
        ("PLAN scheduling + idempotent run_due", plans),
    ]


def run_self_test(out=None) -> int:
    out = out or sys.stdout
    print("bottlenecks conformance self-test (wellmanifest/nl-dsl-llm + uritime/urtlog)", file=out)
    failed = 0
    with tempfile.TemporaryDirectory() as tmp:
        cwd = os.getcwd()
        os.chdir(tmp)
        saved = {k: os.environ.pop(k, None) for k in ("BOTTLENECKS_LLM_URL", "URTLOG_STATE_DIR")}
        try:
            checks = _checks(tmp)
            for name, fn in checks:
                try:
                    ok = bool(fn())
                    err = ""
                except Exception as exc:  # noqa: BLE001
                    ok, err = False, f" ({type(exc).__name__}: {exc})"
                failed += not ok
                print(f"{'✓' if ok else '✗'} {name}{err}", file=out)
        finally:
            os.chdir(cwd)
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v
    total = len(checks)
    print(f"\n{total - failed}/{total} checks passed", file=out)
    return 0 if failed == 0 else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(run_self_test(io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")))
