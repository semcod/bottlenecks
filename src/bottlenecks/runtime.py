"""Runtime bottleneck detection built on the stdlib :mod:`cProfile`."""

from __future__ import annotations

import cProfile
import os
import pstats
import runpy
import sys
from contextlib import contextmanager
from typing import Any, Callable, Iterator, List, Optional, Sequence, Tuple

from .models import Bottleneck, Report


def _label(func: Tuple[str, int, str]) -> str:
    filename, line, name = func
    if filename == "~":
        return name  # builtin
    return f"{filename}:{line} ({name})"


def stats_to_report(stats: pstats.Stats, threshold: float = 0.05, top: int = 20,
                    include_stdlib: bool = False) -> Report:
    """Turn :class:`pstats.Stats` into a ranked report.

    A function is flagged when its *own* time (``tottime``) is at least
    ``threshold`` of total runtime, or when it is called very often.
    """
    raw = stats.stats  # type: ignore[attr-defined]
    total = sum(v[2] for v in raw.values()) or 1e-12
    stdlib = os.path.dirname(os.__file__)
    rows = []
    for func, (cc, nc, tt, ct, _callers) in raw.items():
        filename = func[0]
        if not include_stdlib and (filename.startswith(stdlib) or "cProfile" in filename
                                   or "bottlenecks/runtime" in filename.replace(os.sep, "/")):
            continue
        rows.append((func, cc, nc, tt, ct))
    rows.sort(key=lambda r: r[3], reverse=True)
    report = Report()
    for func, cc, nc, tt, ct in rows[:top]:
        share = tt / total
        if share < threshold:
            continue
        per_call = tt / nc if nc else 0.0
        kind = "hot-function"
        suggestion = "optimise the algorithm, cache results or move work out of the hot path"
        if nc >= 10_000 and per_call < 1e-4:
            kind = "call-overhead"
            suggestion = "inline/vectorise the tiny function or reduce call count (memoise, batch)"
        if cc != nc:
            suggestion += "; function is recursive"
        report.items.append(Bottleneck(
            "runtime", kind, _label(func),
            f"{share:.1%} of own time, {nc} calls, {tt * 1000:.1f} ms self / {ct * 1000:.1f} ms cumulative",
            min(1.0, share * 1.6), suggestion=suggestion,
            metrics={"share": round(share, 4), "calls": nc, "tottime": tt, "cumtime": ct, "per_call": per_call}))
    report.summary["runtime"] = {"total_seconds": round(total, 6), "functions": len(raw),
                                 "findings": len(report.items)}
    return report


def profile_callable(func: Callable[..., Any], *args: Any, threshold: float = 0.05, top: int = 20,
                     **kwargs: Any) -> Tuple[Any, Report]:
    """Run ``func(*args, **kwargs)`` under cProfile and return ``(result, report)``."""
    profiler = cProfile.Profile()
    result = profiler.runcall(func, *args, **kwargs)
    return result, stats_to_report(pstats.Stats(profiler), threshold=threshold, top=top)


class _Holder:
    report: Optional[Report] = None


@contextmanager
def profile(threshold: float = 0.05, top: int = 20) -> Iterator[_Holder]:
    """Context manager: ``with profile() as p: ...; p.report``."""
    holder = _Holder()
    profiler = cProfile.Profile()
    profiler.enable()
    try:
        yield holder
    finally:
        profiler.disable()
        holder.report = stats_to_report(pstats.Stats(profiler), threshold=threshold, top=top)


def profile_script(path: str, argv: Optional[Sequence[str]] = None, threshold: float = 0.05,
                   top: int = 20) -> Report:
    """Execute a Python script as ``__main__`` under cProfile."""
    saved_argv, saved_path = sys.argv[:], sys.path[:]
    sys.argv = [path] + list(argv or [])
    sys.path.insert(0, os.path.dirname(os.path.abspath(path)))
    profiler = cProfile.Profile()
    try:
        profiler.enable()
        try:
            runpy.run_path(path, run_name="__main__")
        except SystemExit:
            pass
        finally:
            profiler.disable()
    finally:
        sys.argv, sys.path[:] = saved_argv, saved_path
    return stats_to_report(pstats.Stats(profiler), threshold=threshold, top=top)


def load_pstats(path: str, threshold: float = 0.05, top: int = 20) -> Report:
    """Analyse a saved ``.prof`` / ``.pstats`` file."""
    return stats_to_report(pstats.Stats(path), threshold=threshold, top=top, include_stdlib=True)


__all__: List[str] = ["profile", "profile_callable", "profile_script", "load_pstats", "stats_to_report"]
