"""Layer 2 – canonical DSL (``wellmanifest/nl-dsl-llm`` compatible).

Text syntax::

    entity.operation [key=value ...] [filter:key=value ...]

e.g. ``code.scan path=src top=5 filter:severity=high``. The DSL is the **only**
execution surface: CLI, REST, MCP and the NL layers all compile to a
:class:`DSLCommand` before anything runs.
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from .models import Report

_IDENTIFIER_RE = re.compile(r"^[a-z0-9_-]+$")

SOURCE_LAYERS = ("direct_dsl", "nl_fast_path", "semantic_cache", "llm_fallback", "plan_executor")
STATUSES = ("OK", "NOT_FOUND", "VALIDATION_ERROR", "EXECUTION_ERROR", "UNAUTHORIZED", "CLARIFY")


def _format_value(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value)
    if not text or any(ch.isspace() or ch in ('"', "'") for ch in text):
        return json.dumps(text, ensure_ascii=False)
    return text


@dataclass
class DSLCommand:
    entity: str
    operation: str
    arguments: Dict[str, Any] = field(default_factory=dict)
    filters: Dict[str, Any] = field(default_factory=dict)
    idempotency_key: Optional[str] = None
    raw_dsl: Optional[str] = None

    @property
    def action_id(self) -> str:
        return f"{self.entity}.{self.operation}"

    def get_param(self, key: str, default: Any = None) -> Any:
        if key in self.filters:
            return self.filters[key]
        return self.arguments.get(key, default)

    def to_canonical_string(self) -> str:
        parts = [self.action_id]
        for key, val in sorted(self.arguments.items()):
            parts.append(f"{key}={_format_value(val)}")
        for key, val in sorted(self.filters.items()):
            parts.append(f"filter:{key}={_format_value(val)}")
        return " ".join(parts)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "entity": self.entity, "operation": self.operation, "arguments": dict(self.arguments),
            "filters": dict(self.filters), "idempotencyKey": self.idempotency_key, "rawDsl": self.raw_dsl,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DSLCommand":
        if not isinstance(data, dict):
            raise ValueError("DSL command envelope must be an object")
        entity, operation = data.get("entity"), data.get("operation")
        if not isinstance(entity, str) or not isinstance(operation, str):
            raise ValueError("DSL command requires string 'entity' and 'operation'")
        if not _IDENTIFIER_RE.fullmatch(entity.lower()) or not _IDENTIFIER_RE.fullmatch(operation.lower()):
            raise ValueError("invalid DSL entity or operation identifier")
        args, filters = data.get("arguments") or {}, data.get("filters") or {}
        if not isinstance(args, dict) or not isinstance(filters, dict):
            raise ValueError("'arguments' and 'filters' must be objects")
        return cls(entity.lower(), operation.lower(), dict(args), dict(filters),
                   data.get("idempotencyKey"), data.get("rawDsl"))

    @classmethod
    def from_text(cls, text: str) -> "DSLCommand":
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Empty DSL command string")
        cleaned = text.strip()
        try:
            tokens = shlex.split(cleaned, posix=True)
        except ValueError as exc:
            raise ValueError(f"Malformed quoted DSL command: {exc}") from exc
        head = tokens[0]
        if "." not in head:
            raise ValueError(f"Invalid DSL command '{cleaned}': expected 'entity.operation [key=value ...]'")
        entity, operation = (s.lower() for s in head.split(".", 1))
        if not _IDENTIFIER_RE.fullmatch(entity) or not _IDENTIFIER_RE.fullmatch(operation):
            raise ValueError(f"Invalid DSL entity or operation identifier in '{cleaned}'")
        args: Dict[str, Any] = {}
        filters: Dict[str, Any] = {}
        for token in tokens[1:]:
            if "=" not in token:
                raise ValueError(f"Invalid DSL parameter token without '=': '{token}'")
            key, val = token.split("=", 1)
            if key.startswith("filter:"):
                if not key[7:]:
                    raise ValueError("Empty filter key in DSL command")
                filters[key[7:]] = val
            elif not key:
                raise ValueError("Empty parameter key in DSL command")
            else:
                args[key] = val
        return cls(entity, operation, args, filters, raw_dsl=cleaned)


@dataclass
class DSLResult:
    """Unified response envelope shared by every interface."""

    success: bool
    status: str
    data: Any
    errors: List[Dict[str, str]] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)
    report: Optional[Report] = field(default=None, repr=False, compare=False)

    def to_dict(self) -> Dict[str, Any]:
        out = asdict(self)
        out.pop("report", None)
        return out


def error_result(status: str, code: str, message: str, source_layer: str, trace_id: str,
                 canonical: Optional[str] = None, field_name: Optional[str] = None, data: Any = None) -> DSLResult:
    err: Dict[str, str] = {"code": code, "message": message}
    if field_name:
        err["field"] = field_name
    return DSLResult(False, status, data, [err], {
        "executionTimeMs": 0.0, "sourceLayer": source_layer, "canonicalDsl": canonical, "traceId": trace_id})
