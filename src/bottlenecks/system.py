"""Host system bottleneck detection (CPU, memory, swap, disk, I/O wait, load).

Uses :mod:`psutil` when installed (``pip install bottlenecks[system]``), otherwise
falls back to ``/proc`` (Linux) and the stdlib.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .models import Bottleneck, Report

try:  # pragma: no cover - optional dependency
    import psutil  # type: ignore
except Exception:  # pragma: no cover
    psutil = None  # type: ignore


def _read_proc_stat() -> Optional[Tuple[float, float, float]]:
    try:
        with open("/proc/stat", encoding="ascii") as fh:
            fields = [float(x) for x in fh.readline().split()[1:]]
    except (OSError, ValueError):
        return None
    idle = fields[3] + (fields[4] if len(fields) > 4 else 0.0)
    iowait = fields[4] if len(fields) > 4 else 0.0
    return sum(fields), idle, iowait


def _cpu_and_iowait(interval: float) -> Tuple[Optional[float], Optional[float]]:
    if psutil is not None:
        times = psutil.cpu_times_percent(interval=interval)
        iowait = getattr(times, "iowait", None)
        return max(0.0, 100.0 - times.idle - (iowait or 0.0)), iowait
    a = _read_proc_stat()
    if a is None:
        return None, None
    time.sleep(interval)
    b = _read_proc_stat()
    if b is None:
        return None, None
    total = b[0] - a[0] or 1.0
    return 100.0 * (1 - (b[1] - a[1]) / total), 100.0 * (b[2] - a[2]) / total


def _meminfo() -> Dict[str, float]:
    if psutil is not None:
        vm, sw = psutil.virtual_memory(), psutil.swap_memory()
        return {"mem_percent": vm.percent, "mem_total": vm.total, "swap_percent": sw.percent,
                "swap_available_bytes": sw.free}
    info: Dict[str, float] = {}
    try:
        with open("/proc/meminfo", encoding="ascii") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                info[key] = float(rest.split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        return {}
    total = info.get("MemTotal", 0.0)
    avail = info.get("MemAvailable", info.get("MemFree", 0.0))
    swap_total = info.get("SwapTotal", 0.0)
    out = {"mem_total": total}
    if total:
        out["mem_percent"] = 100.0 * (1 - avail / total)
    if swap_total:
        swap_free = info.get("SwapFree", 0.0)
        out["swap_available_bytes"] = swap_free
        out["swap_percent"] = 100.0 * (1 - swap_free / swap_total)
    return out


def _top_processes(n: int = 5) -> List[Dict[str, Any]]:
    if psutil is None:
        return []
    procs = []
    for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]):
        procs.append(p.info)
    procs.sort(key=lambda p: ((p.get("cpu_percent") or 0) + (p.get("memory_percent") or 0)), reverse=True)
    return procs[:n]


def _linux_scheduler(proc: Path) -> Dict[str, int]:
    fields = {"procs_running": "runnable_processes", "procs_blocked": "io_blocked_processes"}
    counts = {}
    try:
        with (proc / "stat").open(encoding="ascii") as stream:
            for line in stream:
                label, _, value = line.partition(" ")
                if label in fields and value.strip().isdigit():
                    counts[fields[label]] = int(value)
    except OSError:
        return {}
    return counts


def _linux_pressure(proc: Path) -> Dict[str, Dict[str, float]]:
    pressures = {}
    for resource in ("cpu", "io", "memory"):
        try:
            lines = (proc / "pressure" / resource).read_text(encoding="ascii").splitlines()
        except OSError:
            continue
        average = {}
        for line in lines:
            fields = line.split()
            if not fields or fields[0] not in {"some", "full"}:
                continue
            for field in fields[1:]:
                if field.startswith("avg60="):
                    try:
                        percent = float(field.removeprefix("avg60="))
                    except ValueError:
                        continue
                    if 0.0 <= percent <= 100.0:
                        average[fields[0]] = percent
        if average:
            pressures[resource] = average
    return pressures


def _linux_swap_pages(proc: Path) -> Optional[Tuple[int, int]]:
    counters = {}
    try:
        with (proc / "vmstat").open(encoding="ascii") as stream:
            for line in stream:
                key, _, value = line.partition(" ")
                if key in {"pswpin", "pswpout"}:
                    counters[key] = int(value)
        return counters["pswpin"], counters["pswpout"]
    except (OSError, ValueError, KeyError):
        return None


def _swap_byte_rates(before: Tuple[int, int], after: Tuple[int, int], seconds: float,
                     page_size: int) -> Optional[Dict[str, float]]:
    pages_in = after[0] - before[0]
    pages_out = after[1] - before[1]
    if seconds <= 0 or min(pages_in, pages_out) < 0:
        return None
    return {"in": pages_in * page_size / seconds, "out": pages_out * page_size / seconds}


def collect_metrics(interval: float = 0.5, mounts: Sequence[str] = ("/",)) -> Dict[str, Any]:
    """Collect a snapshot of host metrics."""
    proc = Path("/proc")
    start = time.monotonic()
    swap_before = _linux_swap_pages(proc) if interval > 0 else None
    cpu, iowait = _cpu_and_iowait(interval)
    swap_after = _linux_swap_pages(proc) if swap_before is not None else None
    elapsed = time.monotonic() - start
    metrics: Dict[str, Any] = {"cpu_count": os.cpu_count() or 1, "cpu_percent": cpu, "iowait_percent": iowait}
    try:
        metrics["load_avg"] = os.getloadavg()
    except (OSError, AttributeError):
        metrics["load_avg"] = None
    metrics.update(_meminfo())
    if swap_before is not None and swap_after is not None:
        if rates := _swap_byte_rates(swap_before, swap_after, elapsed, os.sysconf("SC_PAGE_SIZE")):
            metrics["swap_io_bytes_per_second"] = rates
    metrics.update(_linux_scheduler(proc))
    if pressure := _linux_pressure(proc):
        metrics["pressure_avg60_percent"] = pressure
    disks = {}
    available = {}
    for m in mounts:
        try:
            du = shutil.disk_usage(m)
            capacity = du.used + du.free
            disks[m] = 100.0 * du.used / capacity if capacity else 0.0
            available[m] = du.free
        except OSError:
            continue
    metrics["disk_percent"] = disks
    metrics["disk_available_bytes"] = available
    metrics["top_processes"] = _top_processes()
    return metrics


def _ramp(value: float, warn: float, crit: float) -> float:
    """Score 0.35 at ``warn`` rising to 0.9 at ``crit``."""
    if value < warn:
        return 0.0
    return min(1.0, 0.35 + 0.55 * (value - warn) / max(crit - warn, 1e-9))


def analyze_metrics(metrics: Dict[str, Any], cpu_warn: float = 80.0, mem_warn: float = 85.0,
                    disk_warn: float = 90.0, iowait_warn: float = 15.0, swap_warn: float = 30.0) -> Report:
    """Turn a metrics snapshot (see :func:`collect_metrics`) into a report."""
    report = Report()
    items = report.items
    cpu = metrics.get("cpu_percent")
    if cpu is not None and (s := _ramp(cpu, cpu_warn, 98.0)):
        items.append(Bottleneck("system", "cpu-saturation", "cpu", f"CPU busy {cpu:.0f}%", s,
                                suggestion="profile the top CPU consumers, add cores or reduce parallel jobs",
                                metrics={"cpu_percent": cpu}))
    load = metrics.get("load_avg")
    ncpu = metrics.get("cpu_count") or 1
    runnable = metrics.get("runnable_processes")
    if runnable is not None:
        per_core = runnable / ncpu
        if (s := _ramp(per_core, 1.0, 3.0)):
            items.append(Bottleneck("system", "run-queue", "load",
                                    f"{runnable} runnable processes on {ncpu} cores", s,
                                    suggestion="reduce parallel jobs or add CPU capacity",
                                    metrics={"runnable_processes": runnable, "per_core": per_core}))
    elif load:
        per_core = load[0] / ncpu
        if (s := _ramp(per_core, 1.0, 3.0)):
            items.append(Bottleneck("system", "run-queue", "load", f"1-min load {load[0]:.2f} on {ncpu} cores "
                                    f"({per_core:.2f}/core)", s,
                                    suggestion="check runnable tasks and I/O stalls before adjusting parallelism",
                                    metrics={"load_avg": list(load), "per_core": per_core}))
    pressure = metrics.get("pressure_avg60_percent") or {}
    for resource, kind, warn, critical, suggestion in (
        ("io", "io-pressure", 20.0, 60.0, "check writeback, storage latency and competing I/O"),
        ("memory", "memory-stall", 10.0, 35.0, "check reclaim, page faults and active swap traffic"),
    ):
        averages = pressure.get(resource) or {}
        some = averages.get("some", 0.0)
        if (s := _ramp(some, warn, critical)):
            items.append(Bottleneck("system", kind, resource,
                                    f"{resource} stalls affected tasks {some:.1f}% of the last minute (PSI)", s,
                                    suggestion=suggestion, metrics={"avg60_percent": averages}))
    iow = metrics.get("iowait_percent")
    if iow is not None and (s := _ramp(iow, iowait_warn, 50.0)):
        items.append(Bottleneck("system", "io-wait", "disk-io", f"CPU waiting on I/O {iow:.0f}% of the time", s,
                                suggestion="faster storage, fewer fsyncs, batch writes or cache reads",
                                metrics={"iowait_percent": iow}))
    mem = metrics.get("mem_percent")
    if mem is not None and (s := _ramp(mem, mem_warn, 98.0)):
        items.append(Bottleneck("system", "memory-pressure", "memory", f"memory used {mem:.0f}%", s,
                                suggestion="find leaks / large caches, stream data instead of loading it whole",
                                metrics={"mem_percent": mem}))
    swap = metrics.get("swap_percent")
    if swap is not None and (s := _ramp(swap, swap_warn, 90.0)):
        swap_metrics = {"swap_percent": swap}
        for field in ("swap_available_bytes", "swap_io_bytes_per_second"):
            if field in metrics:
                swap_metrics[field] = metrics[field]
        items.append(Bottleneck("system", "swapping", "swap", f"swap used {swap:.0f}%", s,
                                suggestion="check sampled swap traffic and memory pressure before reducing workloads",
                                metrics=swap_metrics))
    for mount, pct in (metrics.get("disk_percent") or {}).items():
        if (s := _ramp(pct, disk_warn, 99.0)):
            items.append(Bottleneck("system", "disk-full", mount, f"filesystem {mount} {pct:.0f}% full", s,
                                    suggestion="clean caches/logs/artifacts or grow the volume",
                                    metrics={"disk_percent": pct}))
    report.summary["system"] = {k: v for k, v in metrics.items() if k != "top_processes"}
    if metrics.get("top_processes"):
        report.summary["system"]["top_processes"] = metrics["top_processes"]
    return report


def analyze_system(interval: float = 0.5, mounts: Sequence[str] = ("/",), **thresholds: float) -> Report:
    """Sample the host and report resource bottlenecks."""
    return analyze_metrics(collect_metrics(interval=interval, mounts=mounts), **thresholds)
