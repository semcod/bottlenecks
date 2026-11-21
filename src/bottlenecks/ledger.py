"""Temporal run ledger compatible with ``uritime/urtlog`` (record schema v2).

Every DSL execution becomes one immutable ``LOG`` record – the urtlog 4-tuple::

    LOG URT(at=…, range=start/end) URI(proc://bottlenecks/code/scan/v1?path=src)
        URN_IN(urn:bottlenecks:input:<key>) URN_OUT(urn:bottlenecks:report:<run>, urn:bottlenecks:finding:<fp>, …)
        STATUS(succeeded)

and scheduled scans are ``PLAN`` records (``kind="intent"``, ``status=planned``)
that :meth:`bottlenecks.engine.Engine.run_due` turns into causal ``LOG`` records
(``causation_id = plan.event_id``), exactly like ``urtlog.executor.UrtExecutor``.

Storage mirrors urtlog: append-only ``events.jsonl`` (the source of truth) plus a
disposable SQLite projection indexed by the prefix families ``time_path``,
``op_path`` and ``res_path``. Findings get stable fingerprint URNs, which makes
regressions (new / resolved / worsened bottlenecks) a cheap indexed query.

If ``URTLOG_STATE_DIR`` is set and ``uritime-urtlog`` is importable, every record
is mirrored into the real urtlog store as well (fail-soft).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import quote, urlencode, urlsplit

from .models import Bottleneck, Report

SCHEMA = "uritime.urtlog.record/v2"
SOURCE = "bottlenecks"
STATE_ENV = "BOTTLENECKS_STATE_DIR"
URTLOG_ENV = "URTLOG_STATE_DIR"
MAX_FINDING_URNS = 50

_SEG_CLEAN_RE = re.compile(r"[^a-z0-9._-]+")
_DURATION_RE = re.compile(r"^(?P<neg>-)?PT(?:(?P<h>\d+)H)?(?:(?P<m>\d+)M)?(?:(?P<s>\d+)S)?$")
_SHORT_DURATION_RE = re.compile(r"^(\d+)\s*(s|sec|secs|m|min|mins|h|hr|hrs|d)?$")
_LINE_RE = re.compile(r":\d+(?=\s*\(|$)")


class LedgerNotConfigured(RuntimeError):
    """Raised by history/plan actions when no state directory was configured."""


# --------------------------------------------------------------------------- time
def now_us() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1_000_000)


def format_utc_us(ts_us: int) -> str:
    dt = datetime.fromtimestamp(ts_us / 1_000_000, tz=timezone.utc)
    if dt.microsecond:
        return dt.strftime("%Y-%m-%dT%H:%M:%S.%f").rstrip("0") + "Z"
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


_FRACTION_RE = re.compile(r"\.(\d+)(?=[+-]\d\d:\d\d$)")


def parse_utc_us(value: str) -> int:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    # Python < 3.11 only accepts 3 or 6 fractional digits; urtlog trims trailing zeros.
    text = _FRACTION_RE.sub(lambda m: "." + (m.group(1) + "000000")[:6], text)
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        raise ValueError(f"timestamp requires explicit UTC offset: {value}")
    return int(dt.astimezone(timezone.utc).timestamp() * 1_000_000)


def time_prefix_path(ts_us: int) -> str:
    dt = datetime.fromtimestamp(ts_us / 1_000_000, tz=timezone.utc)
    return dt.strftime("t/%Y/%m/%d/%H/%M/%S.") + f"{dt.microsecond:06d}"


def parse_duration_seconds(value: Any) -> int:
    """Accept URT ``PT5M`` / ``PT300S`` as well as ``300``, ``5m``, ``1h``."""
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if text in ("", "0", "none"):
        return 0
    m = _DURATION_RE.fullmatch(text.upper())
    if m and any(m.group(k) for k in ("h", "m", "s")):
        total = int(m.group("h") or 0) * 3600 + int(m.group("m") or 0) * 60 + int(m.group("s") or 0)
        return -total if m.group("neg") else total
    m = _SHORT_DURATION_RE.fullmatch(text.lower())
    if m:
        mult = {"s": 1, "sec": 1, "secs": 1, "m": 60, "min": 60, "mins": 60, "h": 3600, "hr": 3600, "hrs": 3600,
                "d": 86400, None: 1}[m.group(2)]
        return int(m.group(1)) * mult
    raise ValueError(f"invalid duration: {value!r} (use PT300S, PT5M, 300, 5m or 1h)")


def format_duration_seconds(seconds: int) -> str:
    return "PT0S" if not seconds else f"{'-' if seconds < 0 else ''}PT{abs(int(seconds))}S"


# ---------------------------------------------------------------- identifiers
def ni_digest(data: bytes) -> str:
    """RFC 6920 ``ni:///sha-256;<b64url>`` content digest (as used by urtlog)."""
    b64 = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode("ascii").rstrip("=")
    return f"ni:///sha-256;{b64}"


def short_hash(text: str, n: int = 16) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:n]


def _clean(seg: str) -> str:
    return _SEG_CLEAN_RE.sub("-", seg.strip().lower()).strip("-") or "_"


def res_prefix_path(urn: str) -> str:
    base = urn.strip().split("@", 1)[0]
    if base.startswith("urn:"):
        parts = [_clean(sub) for token in base[4:].split(":") for sub in token.split("/") if sub]
        return "res/" + "/".join(parts) if parts else "res/unknown"
    if base.startswith("ni:///"):
        algo, _, digest = base[6:].partition(";")
        return f"res/ni/{_clean(algo)}/{_clean(digest[:24])}"
    return "res/" + _clean(base)


def parse_operation_uri(uri: str) -> Dict[str, Any]:
    """Split an Action URI exactly like ``urtlog.dsl.OperationUri.parse``."""
    parsed = urlsplit(uri)
    segments = [s for s in parsed.path.split("/") if s]
    if len(segments) >= 2:
        process, action = "/".join(segments[:-1]), segments[-1]
    elif len(segments) == 1:
        process, action = segments[0], "run"
    else:
        process, action = "root", "run"
    service = (parsed.netloc or "local").lower()
    scheme = parsed.scheme.lower()
    from urllib.parse import parse_qsl

    params = dict(parse_qsl(parsed.query, keep_blank_values=False))
    op_path = "op/" + "/".join([_clean(scheme), _clean(service), *(_clean(p) for p in process.split("/") if p), _clean(action)])
    return {"raw": uri, "op_path": op_path, "scheme": scheme, "service": service, "process": process,
            "action": action, "params": params}


def build_action_uri(base_uri: str, arguments: Dict[str, Any]) -> str:
    """Action URI with RFC 3986 query; urtlog forbids whitespace, ',' and '(' ')' unescaped."""
    items = []
    for key, val in sorted(arguments.items()):
        if isinstance(val, (list, tuple)):
            val = ",".join(str(v) for v in val)
        elif isinstance(val, bool):
            val = "true" if val else "false"
        text = str(val)
        if len(text) > 200:
            text = "sha256-" + short_hash(text, 24)
        items.append((key, text))
    if not items:
        return base_uri
    return base_uri + "?" + urlencode(items, quote_via=quote, safe="/")


def finding_fingerprint(b: Bottleneck) -> str:
    """Stable identity of a finding across runs: line numbers are ignored."""
    location = _LINE_RE.sub("", b.location)
    return short_hash(f"{b.domain}|{b.kind}|{location}")


# --------------------------------------------------------------- record model
def _data_ref(urn: str, role: str, digest: Optional[str] = None, planned: bool = False) -> Dict[str, Any]:
    return {"urn": urn, "res_path": res_prefix_path(urn), "role": role, "digest": digest,
            "sensitivity": "internal", "planned": planned}


def build_record(*, kind: str, uri: str, at_us: int, start_us: Optional[int] = None, end_us: Optional[int] = None,
                 step_seconds: int = 0, inputs: Sequence[Dict[str, Any]] = (), outputs: Sequence[Dict[str, Any]] = (),
                 status: str, run_id: Optional[str] = None, causation_id: Optional[str] = None, summary: str = "",
                 metadata: Optional[Dict[str, Any]] = None, actor: str = "user", repository: Optional[str] = None,
                 event_id: Optional[str] = None) -> Dict[str, Any]:
    """Build a dict identical in shape to ``urtlog.dsl.UrtEventRecord.to_dict()``."""
    if kind not in ("log", "intent"):
        raise ValueError(f"invalid kind {kind}")
    if kind == "intent" and status not in ("planned", "canceled"):
        raise ValueError("intent records must be planned or canceled")
    if step_seconds > 0:
        urt_kind = "rate"
    elif start_us is not None and end_us is not None:
        urt_kind = "interval"
    else:
        urt_kind = "instant"
    urt_uri = (f"urt:v1:interval:{format_utc_us(start_us)}/{format_utc_us(end_us)}"  # type: ignore[arg-type]
               if urt_kind == "interval" else f"urt:v1:instant:{format_utc_us(at_us)}")
    urt = {"kind": urt_kind, "at": format_utc_us(at_us), "at_us": at_us, "time_path": time_prefix_path(at_us),
           "start": format_utc_us(start_us) if start_us is not None else None,
           "end": format_utc_us(end_us) if end_us is not None else None,
           "step_seconds": step_seconds, "anchor": None, "tzid": "UTC", "urt_uri": urt_uri}
    stage = "commissioned" if kind == "intent" else ("started" if status == "started" else "completed")
    op = parse_operation_uri(uri)
    basis = json.dumps({"kind": kind, "source": SOURCE, "urt": urt, "uri": uri,
                        "in": [r["urn"] for r in inputs], "out": [r["urn"] for r in outputs],
                        "status": status, "stage": None, "log_urn": None}, sort_keys=True, separators=(",", ":"))
    event_id = event_id or f"evt-{short_hash(basis)}"
    if causation_id and run_id:
        call_path = f"/{causation_id}/{run_id}/{stage}"
    elif causation_id:
        call_path = f"/{causation_id}/{event_id}"
    elif run_id:
        call_path = f"/{run_id}/{stage}"
    else:
        call_path = f"/{event_id}"
    record = {
        "schema": SCHEMA, "event_id": event_id, "kind": kind, "source": SOURCE, "urt": urt, "uri": op,
        "inputs": list(inputs), "outputs": list(outputs), "status": status, "stage": stage,
        "log_type": "lifecycle", "log_urn": None, "run_id": run_id, "parent_run_id": None, "call_path": call_path,
        "custody": {"commissioned_by": actor, "delegation_chain": [], "executed_by": actor if kind == "log" else SOURCE,
                    "recipients": []},
        "repository": repository, "actor": actor, "sensitivity": "internal", "owner_principal": None,
        "causation_id": causation_id, "summary": summary, "metadata": dict(metadata or {}),
    }
    record["dsl"] = record_to_dsl(record)
    return record


def record_to_dsl(record: Dict[str, Any]) -> str:
    """Canonical urtlog DSL line (``LOG``/``PLAN`` 4-tuple)."""
    u = record["urt"]
    parts = [f"at={u['at']}"]
    if u.get("start") and u.get("end"):
        parts.append(f"range={u['start']}/{u['end']}")
    if u.get("step_seconds"):
        parts.append(f"step={format_duration_seconds(u['step_seconds'])}")
    ins = ", ".join(r["urn"] for r in record["inputs"]) or "none"
    outs = ", ".join(r["urn"] for r in record["outputs"]) or "none"
    prefix = "PLAN" if record["kind"] == "intent" else "LOG"
    return (f"{prefix} URT({', '.join(parts)}) URI({record['uri']['raw']}) URN_IN({ins}) "
            f"URN_OUT({outs}) STATUS({record['status']})")


# ----------------------------------------------------------------- urtlog bridge
class UrtlogBridge:
    """Mirror records into a real ``uritime-urtlog`` store (optional, fail-soft)."""

    def __init__(self, state_dir: str) -> None:
        from urtlog.store import UrtLogStore  # type: ignore

        self.state_dir = state_dir
        self.store = UrtLogStore(state_dir)
        self.errors: List[str] = []

    @classmethod
    def from_env(cls) -> Optional["UrtlogBridge"]:
        raw = os.environ.get(URTLOG_ENV, "").strip()
        if not raw:
            return None
        try:
            return cls(raw)
        except Exception:  # urtlog not installed or store unusable
            return None

    def append(self, record: Dict[str, Any]) -> bool:
        try:
            try:
                from urtlog.store import _record_from_json  # type: ignore

                rec = _record_from_json(json.dumps(record))
            except ImportError:
                from urtlog.dsl import parse_dsl_line  # type: ignore

                rec = parse_dsl_line(record["dsl"], source=SOURCE)
            return bool(self.store.append(rec))
        except Exception as exc:  # noqa: BLE001 - mirroring must never break analysis
            self.errors.append(str(exc))
            return False

    def close(self) -> None:
        try:
            self.store.close()
        except Exception:  # noqa: BLE001
            pass


# ------------------------------------------------------------------- the ledger
_SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS events(
    event_id TEXT PRIMARY KEY, kind TEXT NOT NULL, at_us INTEGER NOT NULL,
    range_start_us INTEGER, range_end_us INTEGER, step_seconds INTEGER NOT NULL DEFAULT 0,
    time_path TEXT NOT NULL, op_path TEXT NOT NULL, uri_raw TEXT NOT NULL,
    action_id TEXT, input_key TEXT, status TEXT NOT NULL, stage TEXT, run_id TEXT,
    causation_id TEXT, summary TEXT, record_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_events_time ON events(time_path);
CREATE INDEX IF NOT EXISTS idx_events_op ON events(op_path, at_us);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind, status, at_us);
CREATE INDEX IF NOT EXISTS idx_events_cause ON events(causation_id);
CREATE INDEX IF NOT EXISTS idx_events_input ON events(action_id, input_key, at_us);
CREATE TABLE IF NOT EXISTS event_urns(
    event_id TEXT NOT NULL, direction TEXT NOT NULL, urn TEXT NOT NULL, res_path TEXT NOT NULL,
    role TEXT, digest TEXT, PRIMARY KEY(event_id, direction, urn));
CREATE INDEX IF NOT EXISTS idx_urns_urn ON event_urns(urn, direction);
CREATE INDEX IF NOT EXISTS idx_urns_res ON event_urns(res_path);
CREATE TABLE IF NOT EXISTS findings(
    run_id TEXT NOT NULL, event_id TEXT NOT NULL, fingerprint TEXT NOT NULL, urn TEXT NOT NULL,
    domain TEXT, kind TEXT, location TEXT, severity TEXT, score REAL, message TEXT, suggestion TEXT,
    PRIMARY KEY(run_id, fingerprint));
CREATE INDEX IF NOT EXISTS idx_findings_fp ON findings(fingerprint);
"""


class Ledger:
    def __init__(self, state_dir: os.PathLike, bridge: Optional[UrtlogBridge] = None, actor: str = "user") -> None:
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.jsonl_path = self.state_dir / "events.jsonl"
        self.db_path = self.state_dir / "ledger.sqlite3"
        self.actor = actor
        self.bridge = bridge if bridge is not None else UrtlogBridge.from_env()
        self.conn = sqlite3.connect(str(self.db_path), timeout=10.0, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA_SQL)
        if self._needs_rebuild():
            self.rebuild()

    @classmethod
    def from_env(cls) -> Optional["Ledger"]:
        raw = os.environ.get(STATE_ENV, "").strip()
        return cls(raw) if raw else None

    def close(self) -> None:
        self.conn.close()
        if self.bridge is not None:
            self.bridge.close()

    # projection ---------------------------------------------------------
    def _needs_rebuild(self) -> bool:
        if not self.jsonl_path.exists():
            return False
        count = self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        return count == 0 and self.jsonl_path.stat().st_size > 0

    def rebuild(self) -> int:
        """Re-project SQLite from the append-only ``events.jsonl`` (source of truth)."""
        with self.conn:
            for table in ("events", "event_urns", "findings"):
                self.conn.execute(f"DELETE FROM {table}")
        n = 0
        with self.jsonl_path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if self._project(rec):
                    n += 1
        return n

    def _project(self, record: Dict[str, Any]) -> bool:
        meta = record.get("metadata") or {}
        u = record["urt"]
        start = parse_utc_us(u["start"]) if u.get("start") else None
        end = parse_utc_us(u["end"]) if u.get("end") else None
        with self.conn:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO events(event_id, kind, at_us, range_start_us, range_end_us, step_seconds,"
                " time_path, op_path, uri_raw, action_id, input_key, status, stage, run_id, causation_id, summary,"
                " record_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (record["event_id"], record["kind"], u["at_us"], start, end, u.get("step_seconds") or 0,
                 u["time_path"], record["uri"]["op_path"], record["uri"]["raw"], meta.get("action_id"),
                 meta.get("input_key"), record["status"], record.get("stage"), record.get("run_id"),
                 record.get("causation_id"), record.get("summary", ""),
                 json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)))
            if cur.rowcount == 0:
                return False
            rows = [(record["event_id"], direction, ref["urn"], ref.get("res_path") or res_prefix_path(ref["urn"]),
                     ref.get("role"), ref.get("digest"))
                    for direction, key in (("in", "inputs"), ("out", "outputs")) for ref in record.get(key) or ()]
            self.conn.executemany("INSERT OR IGNORE INTO event_urns VALUES (?,?,?,?,?,?)", rows)
            for f in meta.get("findings") or ():
                self.conn.execute(
                    "INSERT OR IGNORE INTO findings VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (record.get("run_id"), record["event_id"], f["fingerprint"], f["urn"], f["domain"], f["kind"],
                     f["location"], f["severity"], f["score"], f.get("message", ""), f.get("suggestion", "")))
        return True

    def append(self, record: Dict[str, Any]) -> bool:
        """Append idempotently (same event_id → ignored) to JSONL + projection (+ urtlog mirror)."""
        if not self._project(record):
            return False
        with self.jsonl_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n")
        if self.bridge is not None:
            self.bridge.append(record)
        return True

    # writers ------------------------------------------------------------
    def record_run(self, *, action_id: str, base_uri: str, arguments: Dict[str, Any], canonical: str,
                   input_key: str, start_us: int, end_us: int, status: str, report: Optional[Report] = None,
                   data: Any = None, error: Optional[str] = None, causation_id: Optional[str] = None,
                   source_layer: str = "direct_dsl") -> Dict[str, Any]:
        run_id = f"run-{uuid.uuid4().hex[:12]}"
        uri = build_action_uri(base_uri, arguments)
        inputs = [_data_ref(f"urn:bottlenecks:input:{input_key}", "command", ni_digest(canonical.encode("utf-8")))]
        outputs: List[Dict[str, Any]] = []
        findings: List[Dict[str, Any]] = []
        meta: Dict[str, Any] = {"action_id": action_id, "input_key": input_key, "canonical_dsl": canonical,
                                "source_layer": source_layer}
        if status == "succeeded":
            payload = report.to_dict() if report is not None else data
            body = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
            outputs.append(_data_ref(f"urn:bottlenecks:report:{run_id}", "report", ni_digest(body)))
        if report is not None:
            seen: Dict[str, int] = {}
            for b in report.ranked():
                fp = finding_fingerprint(b)
                seen[fp] = seen.get(fp, 0) + 1
                if seen[fp] > 1:
                    fp = f"{fp}-{seen[fp]}"
                urn = f"urn:bottlenecks:finding:{fp}"
                findings.append({"fingerprint": fp, "urn": urn, "domain": b.domain, "kind": b.kind,
                                 "location": b.location, "severity": b.severity, "score": round(b.score, 4),
                                 "message": b.message, "suggestion": b.suggestion})
            outputs.extend(_data_ref(f["urn"], f"finding:{f['severity']}") for f in findings[:MAX_FINDING_URNS])
            meta.update(findings=findings, worst_severity=report.worst_severity(), finding_count=len(findings))
        if error:
            meta["error"] = error[:500]
        summary = (f"{action_id}: {len(findings)} finding(s), worst={meta.get('worst_severity')}"
                   if report is not None else f"{action_id}: {status}")
        record = build_record(kind="log", uri=uri, at_us=start_us, start_us=start_us,
                              end_us=max(end_us, start_us + 1), inputs=inputs, outputs=outputs, status=status,
                              run_id=run_id, causation_id=causation_id, summary=summary, metadata=meta,
                              actor=self.actor)
        self.append(record)
        return record

    def schedule(self, *, action_id: str, base_uri: str, arguments: Dict[str, Any], canonical: str,
                 at_us: int, step_seconds: int = 0, window_seconds: int = 0) -> Dict[str, Any]:
        window = window_seconds or max(step_seconds, 60)
        input_key = short_hash(canonical)
        record = build_record(
            kind="intent", uri=build_action_uri(base_uri, arguments), at_us=at_us, start_us=at_us,
            end_us=at_us + window * 1_000_000, step_seconds=step_seconds,
            inputs=[_data_ref(f"urn:bottlenecks:input:{input_key}", "command",
                              ni_digest(canonical.encode("utf-8")), planned=True)],
            outputs=[_data_ref(f"urn:bottlenecks:report:planned-{input_key}-{at_us}", "report", planned=True)],
            status="planned", summary=f"PLAN {canonical}", actor=self.actor,
            metadata={"action_id": action_id, "input_key": input_key, "canonical_dsl": canonical,
                      "window_seconds": window})
        self.append(record)
        return record

    def cancel(self, plan: Dict[str, Any], reason: str, at: Optional[int] = None) -> Dict[str, Any]:
        t = at or now_us()
        record = build_record(kind="log", uri=plan["uri"]["raw"], at_us=t, inputs=plan["inputs"], outputs=(),
                              status="canceled", causation_id=plan["event_id"], summary=f"canceled: {reason}",
                              metadata={"action_id": (plan.get("metadata") or {}).get("action_id"), "reason": reason},
                              actor=self.actor)
        self.append(record)
        return record

    # readers ------------------------------------------------------------
    def _records(self, sql: str, params: Iterable[Any]) -> List[Dict[str, Any]]:
        return [json.loads(r["record_json"]) for r in self.conn.execute(sql, tuple(params))]

    def get(self, event_id: str) -> Optional[Dict[str, Any]]:
        rows = self._records("SELECT record_json FROM events WHERE event_id=?", (event_id,))
        return rows[0] if rows else None

    def query(self, *, kind: Optional[str] = None, status: Optional[str] = None, op_prefix: Optional[str] = None,
              time_prefix: Optional[str] = None, res_prefix: Optional[str] = None, action_id: Optional[str] = None,
              limit: int = 50) -> List[Dict[str, Any]]:
        where, params = [], []
        for col, val in (("kind", kind), ("status", status), ("action_id", action_id)):
            if val:
                where.append(f"e.{col}=?")
                params.append(val)
        for col, prefix in (("e.op_path", op_prefix), ("e.time_path", time_prefix)):
            if prefix:
                where.append(f"{col} >= ? AND {col} < ?")
                params += [prefix, prefix[:-1] + chr(ord(prefix[-1]) + 1)]
        if res_prefix:
            where.append("e.event_id IN (SELECT event_id FROM event_urns WHERE res_path >= ? AND res_path < ?)")
            params += [res_prefix, res_prefix[:-1] + chr(ord(res_prefix[-1]) + 1)]
        sql = "SELECT e.record_json FROM events e" + (" WHERE " + " AND ".join(where) if where else "")
        sql += " ORDER BY e.at_us DESC LIMIT ?"
        return self._records(sql, params + [int(limit)])

    def runs(self, action_id: Optional[str] = None, limit: int = 20) -> List[Dict[str, Any]]:
        out = []
        for rec in self.query(kind="log", action_id=action_id, limit=limit):
            m = rec.get("metadata") or {}
            out.append({"run_id": rec.get("run_id"), "event_id": rec["event_id"], "at": rec["urt"]["at"],
                        "action": m.get("action_id"), "status": rec["status"], "dsl": m.get("canonical_dsl"),
                        "findings": m.get("finding_count"), "worst": m.get("worst_severity"),
                        "duration_ms": _duration_ms(rec), "cause": rec.get("causation_id")})
        return out

    def findings(self, run_id: str) -> List[Dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM findings WHERE run_id=? ORDER BY score DESC", (run_id,))
        return [dict(r) for r in rows]

    def _run_row(self, ref: Optional[str]) -> Optional[sqlite3.Row]:
        if not ref:
            return None
        return self.conn.execute("SELECT * FROM events WHERE kind='log' AND (run_id=? OR event_id=?)",
                                 (ref, ref)).fetchone()

    def diff(self, base: Optional[str] = None, head: Optional[str] = None,
             action_id: Optional[str] = None) -> Dict[str, Any]:
        """Compare two runs; defaults to the two latest succeeded runs with the same action + input."""
        head_row = self._run_row(head)
        if head_row is None:
            sql = "SELECT * FROM events WHERE kind='log' AND status='succeeded' AND run_id IS NOT NULL"
            params: List[Any] = []
            if action_id:
                sql += " AND action_id=?"
                params.append(action_id)
            head_row = self.conn.execute(sql + " ORDER BY at_us DESC LIMIT 1", params).fetchone()
        if head_row is None:
            raise LookupError("no recorded runs to compare")
        base_row = self._run_row(base)
        if base_row is None:
            base_row = self.conn.execute(
                "SELECT * FROM events WHERE kind='log' AND status='succeeded' AND action_id=? AND input_key=?"
                " AND at_us < ? ORDER BY at_us DESC LIMIT 1",
                (head_row["action_id"], head_row["input_key"], head_row["at_us"])).fetchone()
        head_f = {f["fingerprint"]: f for f in self.findings(head_row["run_id"])}
        base_f = {f["fingerprint"]: f for f in self.findings(base_row["run_id"])} if base_row else {}
        new = [f for fp, f in head_f.items() if fp not in base_f]
        resolved = [f for fp, f in base_f.items() if fp not in head_f]
        worsened, improved, persisting = [], [], []
        for fp in head_f.keys() & base_f.keys():
            delta = head_f[fp]["score"] - base_f[fp]["score"]
            item = dict(head_f[fp], previous_score=base_f[fp]["score"], delta=round(delta, 4))
            (worsened if delta >= 0.05 else improved if delta <= -0.05 else persisting).append(item)
        key = lambda f: -f["score"]  # noqa: E731
        return {
            "base": base_row["run_id"] if base_row else None, "head": head_row["run_id"],
            "action": head_row["action_id"], "input_key": head_row["input_key"],
            "new": sorted(new, key=key), "resolved": sorted(resolved, key=key),
            "worsened": sorted(worsened, key=key), "improved": sorted(improved, key=key),
            "persisting": len(persisting),
        }

    def trend(self, action_id: Optional[str] = None, limit: int = 20) -> List[Dict[str, Any]]:
        sql = ("SELECT e.run_id, e.at_us, e.action_id, e.input_key, COUNT(f.fingerprint) AS findings,"
               " ROUND(COALESCE(SUM(f.score),0),4) AS total_score, ROUND(COALESCE(MAX(f.score),0),4) AS max_score"
               " FROM events e LEFT JOIN findings f ON f.run_id=e.run_id"
               " WHERE e.kind='log' AND e.status='succeeded' AND e.run_id IS NOT NULL")
        params: List[Any] = []
        if action_id:
            sql += " AND e.action_id=?"
            params.append(action_id)
        sql += " GROUP BY e.run_id ORDER BY e.at_us DESC LIMIT ?"
        rows = [dict(r, at=format_utc_us(r["at_us"])) for r in self.conn.execute(sql, params + [int(limit)])]
        return list(reversed(rows))

    def pending_plans(self) -> List[Dict[str, Any]]:
        return self._records(
            "SELECT record_json FROM events p WHERE p.kind='intent' AND p.status='planned' AND NOT EXISTS"
            " (SELECT 1 FROM events l WHERE l.kind='log' AND l.causation_id=p.event_id) ORDER BY p.at_us", ())

    def lineage(self, urn: str) -> Dict[str, Any]:
        """Which run produced a URN (``out``) and which runs consumed it (``in``)."""
        res: Dict[str, Any] = {"urn": urn}
        for direction in ("out", "in"):
            rows = self.conn.execute(
                "SELECT e.event_id, e.run_id, e.action_id, e.at_us, e.status FROM event_urns u JOIN events e"
                " ON e.event_id=u.event_id WHERE u.urn=? AND u.direction=? ORDER BY e.at_us", (urn, direction))
            res["produced_by" if direction == "out" else "consumed_by"] = [
                dict(r, at=format_utc_us(r["at_us"])) for r in rows]
        return res


def _duration_ms(record: Dict[str, Any]) -> Optional[float]:
    u = record["urt"]
    if u.get("start") and u.get("end"):
        return round((parse_utc_us(u["end"]) - parse_utc_us(u["start"])) / 1000, 3)
    return None


def findings_to_report(items: Iterable[Dict[str, Any]], summary: Optional[Dict[str, Any]] = None) -> Report:
    report = Report(summary=dict(summary or {}))
    for f in items:
        report.items.append(Bottleneck(f["domain"], f["kind"], f["location"], f.get("message", ""), f["score"],
                                       severity=f["severity"], suggestion=f.get("suggestion", ""),
                                       metrics={k: f[k] for k in ("fingerprint", "urn", "previous_score", "delta")
                                                if k in f}))
    return report


def load_events(source: os.PathLike) -> List[Dict[str, Any]]:
    """Load urtlog/bottlenecks records from a state dir (``events.jsonl`` inside) or a JSONL file."""
    path = Path(source)
    if path.is_dir():
        path = path / "events.jsonl"
    if not path.exists():
        raise FileNotFoundError(f"no events.jsonl at {path}")
    records = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict) and "urt" in rec and "uri" in rec:
                records.append(rec)
    return records


__all__: Tuple[str, ...] = (
    "Ledger", "LedgerNotConfigured", "UrtlogBridge", "build_record", "record_to_dsl", "load_events",
    "finding_fingerprint", "findings_to_report", "parse_duration_seconds", "now_us", "format_utc_us", "parse_utc_us",
)
