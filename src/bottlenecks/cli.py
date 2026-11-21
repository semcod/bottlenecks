"""Command line interface: ``bottlenecks {code,run,pstats,pipeline,system,scan}``."""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional

from . import __version__
from .code import analyze_code
from .models import SEVERITIES, Report, severity_rank
from .pipeline import analyze_pipeline
from .render import RENDERERS, render
from .runtime import load_pstats, profile_script
from .system import analyze_system


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("-f", "--format", choices=sorted(RENDERERS), default="text")
    p.add_argument("--min-severity", choices=SEVERITIES, default=None, help="hide findings below this severity")
    p.add_argument("--top", type=int, default=None, help="show at most N findings")
    p.add_argument("--fail-on", choices=SEVERITIES, default=None,
                   help="exit with code 1 when a finding reaches this severity (CI gate)")
    p.add_argument("-o", "--output", default=None, help="write the report to a file")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bottlenecks", description="Find bottlenecks in code, runtime, pipelines and systems.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("code", help="static analysis of Python sources")
    p.add_argument("paths", nargs="+")
    p.add_argument("--complexity", type=int, default=10)
    p.add_argument("--max-lines", type=int, default=80)
    _common(p)

    p = sub.add_parser("run", help="profile a Python script with cProfile")
    p.add_argument("script")
    p.add_argument("args", nargs=argparse.REMAINDER)
    p.add_argument("--threshold", type=float, default=0.05, help="min share of total time to report")
    _common(p)

    p = sub.add_parser("pstats", help="analyse a saved cProfile .prof file")
    p.add_argument("file")
    p.add_argument("--threshold", type=float, default=0.05)
    _common(p)

    p = sub.add_parser("pipeline", help="analyse a pipeline/DAG JSON spec (tasks and/or stages)")
    p.add_argument("spec", help="JSON file, or '-' for stdin")
    _common(p)

    p = sub.add_parser("system", help="sample host resources")
    p.add_argument("--interval", type=float, default=0.5)
    p.add_argument("--mount", action="append", default=None)
    _common(p)

    p = sub.add_parser("scan", help="code + system in one go")
    p.add_argument("paths", nargs="*", default=["."])
    p.add_argument("--interval", type=float, default=0.5)
    _common(p)
    return parser


def run(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    report: Report
    if args.command == "code":
        report = analyze_code(args.paths, complexity_threshold=args.complexity, length_threshold=args.max_lines)
    elif args.command == "run":
        report = profile_script(args.script, args.args, threshold=args.threshold)
    elif args.command == "pstats":
        report = load_pstats(args.file, threshold=args.threshold)
    elif args.command == "pipeline":
        text = sys.stdin.read() if args.spec == "-" else open(args.spec, encoding="utf-8").read()
        report = analyze_pipeline(json.loads(text))
    elif args.command == "system":
        report = analyze_system(interval=args.interval, mounts=args.mount or ["/"])
    else:  # scan
        report = analyze_code(args.paths).merge(analyze_system(interval=args.interval))
    output = render(report, args.format, min_severity=args.min_severity, top=args.top)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(output + ("" if output.endswith("\n") else "\n"))
    else:
        print(output)
    if args.fail_on and any(severity_rank(b.severity) >= severity_rank(args.fail_on) for b in report.items):
        return 1
    return 0


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":  # pragma: no cover
    main()
