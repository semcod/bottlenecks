"""CLI – one of the parity interfaces over the DSL engine.

Classic subcommands (``code``, ``run``, ``pstats``, ``pipeline``, ``system``,
``scan``, ``urtlog``) are thin DSL builders; ``dsl``/``ask``/``suggest``/``shell``
expose the canonical DSL and the NL layers; ``serve-rest``/``serve-mcp`` start
the other interfaces; ``history``/``plan`` use the urtlog-compatible ledger.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, List, Optional

from . import __version__
from .dsl import DSLCommand, DSLResult
from .engine import Engine
from .models import SEVERITIES, severity_rank
from .render import RENDERERS, render


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("-f", "--format", choices=sorted(RENDERERS), default="text")
    p.add_argument("--min-severity", choices=SEVERITIES, default=None, help="hide findings below this severity")
    p.add_argument("--top", type=int, default=None, help="show at most N findings")
    p.add_argument("--fail-on", choices=SEVERITIES, default=None,
                   help="exit with code 1 when a finding reaches this severity (CI gate)")
    p.add_argument("-o", "--output", default=None, help="write the report to a file")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bottlenecks",
        description="Find bottlenecks in code, runtime, pipelines and systems (NL -> DSL -> engine, urtlog ledger).")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--state", default=None,
                        help="ledger directory (urtlog-compatible events.jsonl); default $BOTTLENECKS_STATE_DIR")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("code", help="static analysis of Python sources  [code.scan]")
    p.add_argument("paths", nargs="+")
    p.add_argument("--complexity", type=int, default=10)
    p.add_argument("--max-lines", type=int, default=80)
    _common(p)

    p = sub.add_parser("run", help="profile a Python script with cProfile  [runtime.profile]")
    p.add_argument("script")
    p.add_argument("args", nargs=argparse.REMAINDER)
    p.add_argument("--threshold", type=float, default=0.05, help="min share of total time to report")
    _common(p)

    p = sub.add_parser("pstats", help="analyse a saved cProfile .prof file  [runtime.pstats]")
    p.add_argument("file")
    p.add_argument("--threshold", type=float, default=0.05)
    _common(p)

    p = sub.add_parser("pipeline", help="analyse a pipeline/DAG JSON spec  [pipeline.analyze]")
    p.add_argument("spec", help="JSON file, or '-' for stdin")
    _common(p)

    p = sub.add_parser("urtlog", help="bottlenecks in recorded executions (urtlog events.jsonl)  [pipeline.urtlog]")
    p.add_argument("source", help="urtlog/bottlenecks state dir or events.jsonl")
    p.add_argument("--op-prefix", default=None)
    _common(p)

    p = sub.add_parser("system", help="sample host resources  [system.sample]")
    p.add_argument("--interval", type=float, default=0.5)
    p.add_argument("--mount", action="append", default=None)
    _common(p)

    p = sub.add_parser("scan", help="code + system in one go  [scan.all]")
    p.add_argument("paths", nargs="*", default=["."])
    p.add_argument("--interval", type=float, default=0.5)
    _common(p)

    p = sub.add_parser("dsl", help="execute a canonical DSL command, e.g. \"code.scan path=src top=5\"")
    p.add_argument("statement", nargs="+")
    _common(p)

    p = sub.add_parser("ask", help="natural language (PL/EN), e.g. \"pokaż krytyczne wąskie gardła w kodzie src\"")
    p.add_argument("query", nargs="+")
    p.add_argument("--no-llm", action="store_true", help="disable the LLM fallback layer")
    p.add_argument("-y", "--yes", action="store_true", help="confirm actions with process/write effects")
    _common(p)

    p = sub.add_parser("suggest", help="autocomplete / option network for a partial query")
    p.add_argument("partial", nargs="+")
    p.add_argument("--max", type=int, default=5)

    p = sub.add_parser("shell", help="interactive REPL (DSL or natural language)")
    p.add_argument("--no-llm", action="store_true")

    p = sub.add_parser("history", help="recorded runs, regressions and trends (needs --state)")
    p.add_argument("what", nargs="?", choices=("list", "diff", "trend", "lineage"), default="list")
    p.add_argument("--action", default=None, help="restrict to an action id, e.g. code.scan")
    p.add_argument("--base", default=None)
    p.add_argument("--head", default=None)
    p.add_argument("--urn", default=None)
    p.add_argument("--limit", type=int, default=20)
    _common(p)

    p = sub.add_parser("plan", help="schedule DSL commands as urtlog PLAN records (needs --state)")
    p.add_argument("what", choices=("schedule", "run-due", "list"))
    p.add_argument("statement", nargs="*", help="DSL command to schedule")
    p.add_argument("--every", default=None, help="PT300S / 5m / 1h")
    p.add_argument("--at", default=None, help="RFC3339 first run")
    p.add_argument("--allow-process", action="store_true")

    sub.add_parser("registry", help="print the action registry (nl-api-llm manifest)")

    p = sub.add_parser("serve-rest", help="loopback REST API (/dsl /query /suggest /schema /history)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)

    sub.add_parser("serve-mcp", help="MCP server (JSON-RPC 2.0 over stdio)")
    sub.add_parser("self-test", help="run the nl-dsl-llm / urtlog conformance self-test")
    return parser


def _dsl(action_id: str, **args: Any) -> DSLCommand:
    entity, operation = action_id.split(".", 1)
    return DSLCommand(entity, operation, {k: v for k, v in args.items() if v is not None})


def _legacy_command(args: argparse.Namespace) -> Optional[DSLCommand]:
    present = {"min_severity": getattr(args, "min_severity", None), "top": getattr(args, "top", None)}
    c = args.command
    if c == "code":
        return _dsl("code.scan", path=args.paths, complexity=args.complexity, max_lines=args.max_lines, **present)
    if c == "run":
        return _dsl("runtime.profile", script=args.script, args=args.args or None, threshold=args.threshold, **present)
    if c == "pstats":
        return _dsl("runtime.pstats", file=args.file, threshold=args.threshold, **present)
    if c == "pipeline":
        spec = sys.stdin.read() if args.spec == "-" else args.spec
        return _dsl("pipeline.analyze", spec=spec, **present)
    if c == "urtlog":
        return _dsl("pipeline.urtlog", source=args.source, op_prefix=args.op_prefix, **present)
    if c == "system":
        return _dsl("system.sample", interval=args.interval, mounts=args.mount, **present)
    if c == "scan":
        return _dsl("scan.all", path=args.paths, interval=args.interval, **present)
    if c == "history":
        if args.what == "list":
            return _dsl("history.list", limit=args.limit, action=args.action)
        if args.what == "trend":
            return _dsl("history.trend", limit=args.limit, action=args.action)
        if args.what == "lineage":
            return _dsl("history.lineage", urn=args.urn)
        return _dsl("history.diff", base=args.base, head=args.head, action=args.action, **present)
    return None


def _emit(text: str, output: Optional[str]) -> None:
    if output:
        with open(output, "w", encoding="utf-8") as fh:
            fh.write(text + ("" if text.endswith("\n") else "\n"))
    else:
        print(text)


def _print_result(res: DSLResult, fmt: str, output: Optional[str], envelope: bool) -> None:
    if not res.success:
        if fmt == "json":
            _emit(json.dumps(res.to_dict(), indent=2, ensure_ascii=False, default=str), output)
            return
        for err in res.errors:
            print(f"error [{res.status}/{err['code']}]: {err['message']}", file=sys.stderr)
        clarify = res.data.get("clarify") if isinstance(res.data, dict) else None
        if clarify and clarify.get("options"):
            print("did you mean:", file=sys.stderr)
            for opt in clarify["options"]:
                print(f"  bottlenecks dsl \"{opt}\"", file=sys.stderr)
        return
    if envelope and fmt == "json":
        _emit(json.dumps(res.to_dict(), indent=2, ensure_ascii=False, default=str), output)
    elif res.report is not None:
        _emit(render(res.report, fmt), output)
    else:
        _emit(json.dumps(res.data, indent=2, ensure_ascii=False, default=str), output)


def _exit_code(res: DSLResult, fail_on: Optional[str]) -> int:
    if not res.success:
        return 2
    if fail_on and res.report is not None and any(
            severity_rank(b.severity) >= severity_rank(fail_on) for b in res.report.items):
        return 1
    return 0


def _shell(engine: Engine, allow_llm: bool) -> int:  # pragma: no cover - interactive
    print(f"bottlenecks {__version__} shell – type DSL (code.scan path=src) or a question; 'help', 'quit'")
    while True:
        try:
            line = input("bottlenecks> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if line in ("quit", "exit", "q"):
            return 0
        if not line:
            continue
        if line in ("help", "?"):
            for a in engine.registry:
                print(f"  {a.id:<18} {a.title}")
            continue
        if line.endswith("?") and " " not in line.strip("?"):
            for o in engine.suggest(line.rstrip("?"))["suggestions"]:
                print(f"  {o['completion_text']:<28} {o['display_label']}")
            continue
        res = engine.ask(line, allow_llm_fallback=allow_llm, confirmed=False)
        if res.status == "UNAUTHORIZED" and isinstance(res.data, dict) and res.data.get("requiresConfirmation"):
            if input(f"{res.meta.get('actionId')} executes code / writes state – run it? [y/N] ").lower() == "y":
                res = engine.ask(line, allow_llm_fallback=allow_llm, confirmed=True)
        if res.success and res.meta.get("canonicalDsl"):
            print(f"# {res.meta['sourceLayer']}: {res.meta['canonicalDsl']}")
        _print_result(res, "text", None, envelope=False)


def run(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "self-test":
        from .conformance import run_self_test

        return run_self_test()
    engine = Engine.from_env(args.state)
    try:
        c = args.command
        if c == "registry":
            print(json.dumps(engine.registry.manifest(), indent=2, ensure_ascii=False))
            return 0
        if c == "suggest":
            print(json.dumps(engine.suggest(" ".join(args.partial), max_suggestions=args.max), indent=2,
                             ensure_ascii=False))
            return 0
        if c == "shell":
            return _shell(engine, not args.no_llm)
        if c == "serve-rest":
            from .interfaces import serve_rest

            serve_rest(engine, args.host, args.port)
            return 0
        if c == "serve-mcp":
            from .interfaces import serve_mcp

            serve_mcp(engine)
            return 0
        if c == "plan":
            if args.what == "schedule":
                if not args.statement:
                    print("error: plan schedule needs a DSL statement", file=sys.stderr)
                    return 2
                cmd = _dsl("plan.schedule", command=" ".join(args.statement), every=args.every, at=args.at,
                           allow_process=args.allow_process or None)
            else:
                cmd = _dsl("plan.run_due" if args.what == "run-due" else "plan.list")
            res = engine.execute(cmd, confirmed=True)
            _print_result(res, "text", None, envelope=False)
            return _exit_code(res, None)
        envelope = c in ("dsl", "ask")
        if c == "dsl":
            res = engine.execute(" ".join(args.statement), source_layer="direct_dsl", confirmed=True)
        elif c == "ask":
            res = engine.ask(" ".join(args.query), allow_llm_fallback=not args.no_llm, confirmed=args.yes)
            if res.success and res.meta.get("canonicalDsl") and args.format != "json":
                print(f"# {res.meta['sourceLayer']}: {res.meta['canonicalDsl']}", file=sys.stderr)
        else:
            cmd = _legacy_command(args)
            assert cmd is not None
            res = engine.execute(cmd, source_layer="direct_dsl", confirmed=True)
        _print_result(res, getattr(args, "format", "text"), getattr(args, "output", None), envelope)
        return _exit_code(res, getattr(args, "fail_on", None))
    finally:
        engine.close()


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":  # pragma: no cover
    main()
