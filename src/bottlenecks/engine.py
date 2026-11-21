"""The engine: Layer 2 single execution boundary + NL bridge + plan executor.

``Engine.execute`` is the only place where an action runs. It validates the
command against the action registry (unknown actions/parameters are rejected),
enforces confirmation for side-effecting actions, wraps the result in the
unified envelope and records an urtlog-compatible ``LOG`` record in the ledger.
``Engine.ask`` chains the NL layers (1 → 1.5 → 3) in front of it.
"""

from __future__ import annotations

import difflib
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .actions import build_registry
from .dsl import DSLCommand, DSLResult, error_result
from .ledger import Ledger, LedgerNotConfigured, now_us, short_hash
from .models import Report
from .nl import IntentParser, LLMTranslator, SemanticCache, load_lexicon
from .registry import Action, ActionRegistry
from .suggest import OptionNetwork

PRESENTATION = ("min_severity", "top")
FILTERS = ("severity", "domain", "kind")


class Engine:
    def __init__(self, ledger: Optional[Ledger] = None, translator: Optional[LLMTranslator] = None,
                 lexicon: Optional[Dict[str, Any]] = None, registry: Optional[ActionRegistry] = None) -> None:
        self.ledger = ledger
        self.registry = registry or build_registry(self)
        self.lexicon = lexicon or load_lexicon()
        self.parser = IntentParser(self.registry, self.lexicon)
        cache_path = Path(ledger.state_dir) / "nl-cache.json" if ledger is not None else None
        self.cache = SemanticCache(self.parser, path=cache_path)
        self.translator = translator if translator is not None else LLMTranslator.from_env(self.registry)
        self.options = OptionNetwork(self.registry, self.lexicon)

    @classmethod
    def from_env(cls, state_dir: Optional[str] = None) -> "Engine":
        ledger = Ledger(state_dir) if state_dir else Ledger.from_env()
        return cls(ledger=ledger)

    def close(self) -> None:
        if self.ledger is not None:
            self.ledger.close()

    # ------------------------------------------------------------ compile
    def compile(self, command: Any) -> Tuple[DSLCommand, Action, Dict[str, Any]]:
        """Parse + validate a DSL string/envelope/command; raise ``ValueError``/``LookupError`` on failure."""
        if isinstance(command, DSLCommand):
            cmd = command
        elif isinstance(command, dict):
            cmd = DSLCommand.from_dict(command)
        else:
            cmd = DSLCommand.from_text(str(command))
        action = self.registry.get(cmd.action_id)
        if action is None:
            close = difflib.get_close_matches(cmd.action_id, self.registry.ids(), n=3)
            raise LookupError(f"unknown action '{cmd.action_id}'" + (f"; did you mean {close}?" if close else ""))
        unknown_filters = sorted(set(cmd.filters) - set(FILTERS))
        if unknown_filters:
            raise ValueError(f"unknown filter(s): {', '.join(unknown_filters)} (allowed: {', '.join(FILTERS)})")
        try:
            bound = action.bind(dict(cmd.arguments))
        except KeyError as exc:
            raise ValueError(f"unknown parameter(s) for {action.id}: {exc.args[0]}") from None
        except LookupError as exc:
            raise ValueError(f"missing required parameter '{exc.args[0]}' for {action.id}") from None
        return cmd, action, bound

    # ------------------------------------------------------------ execute
    def execute(self, command: Any, source_layer: str = "direct_dsl", confirmed: bool = True,
                causation_id: Optional[str] = None) -> DSLResult:
        trace = uuid.uuid4().hex[:8]
        t0 = time.perf_counter()
        raw = command.to_canonical_string() if isinstance(command, DSLCommand) else (
            None if isinstance(command, dict) else str(command))
        try:
            cmd, action, bound = self.compile(command)
        except LookupError as exc:
            return error_result("NOT_FOUND", "DSL_UNKNOWN_ACTION", str(exc), source_layer, trace, raw)
        except ValueError as exc:
            return error_result("VALIDATION_ERROR", "DSL_INVALID_COMMAND", str(exc), source_layer, trace, raw)
        canonical = cmd.to_canonical_string()
        meta: Dict[str, Any] = {"executionTimeMs": 0.0, "sourceLayer": source_layer, "canonicalDsl": canonical,
                                "traceId": trace, "actionId": action.id, "actionUri": action.uri,
                                "effects": list(action.effects)}
        if action.requires_confirmation and not confirmed:
            return DSLResult(False, "UNAUTHORIZED", {"requiresConfirmation": True}, [{
                "code": "CONFIRMATION_REQUIRED",
                "message": f"{action.id} has effects {list(action.effects)}; re-send with confirm=true"}], meta)
        presentation = {k: bound.pop(k) for k in PRESENTATION if k in bound}
        call_args = dict(bound)
        input_key = short_hash(action.id + "|" + "|".join(f"{k}={call_args[k]}" for k in sorted(call_args)))
        started = now_us()
        status, data, report, error = "succeeded", None, None, None
        try:
            out = action.handler(**call_args)
        except LedgerNotConfigured as exc:
            meta["executionTimeMs"] = round((time.perf_counter() - t0) * 1000, 3)
            return DSLResult(False, "VALIDATION_ERROR", None, [{"code": "LEDGER_NOT_CONFIGURED", "message": str(exc)}],
                             meta)
        except PermissionError as exc:
            meta["executionTimeMs"] = round((time.perf_counter() - t0) * 1000, 3)
            return DSLResult(False, "UNAUTHORIZED", None, [{"code": "EFFECT_NOT_ALLOWED", "message": str(exc)}], meta)
        except Exception as exc:  # noqa: BLE001 - handler boundary converts exceptions to the envelope
            status, error = "failed", f"{type(exc).__name__}: {exc}"
            out = None
        ended = now_us()
        if isinstance(out, Report):
            report = out
            data = self._present(report, cmd.filters, presentation)
        else:
            data = out
        if self.ledger is not None and action.entity not in ("history", "registry", "plan"):
            try:
                rec = self.ledger.record_run(action_id=action.id, base_uri=action.uri, arguments={**call_args},
                                             canonical=canonical, input_key=input_key, start_us=started,
                                             end_us=ended, status=status, report=report, data=data, error=error,
                                             causation_id=causation_id, source_layer=source_layer)
                meta.update(eventId=rec["event_id"], runId=rec["run_id"], urtlog=rec["dsl"])
            except Exception as exc:  # noqa: BLE001 - ledger must never hide the analysis result
                meta["ledgerError"] = str(exc)
        meta["executionTimeMs"] = round((time.perf_counter() - t0) * 1000, 3)
        if status == "failed":
            return DSLResult(False, "EXECUTION_ERROR", None, [{"code": "HANDLER_EXCEPTION", "message": error or ""}],
                             meta)
        filtered = None
        if report is not None:
            filtered = Report(items=_filter_items(report, cmd.filters, presentation), summary=report.summary)
        return DSLResult(True, "OK", data, [], meta, report=filtered)

    @staticmethod
    def _present(report: Report, filters: Dict[str, Any], presentation: Dict[str, Any]) -> Dict[str, Any]:
        data = report.to_dict()
        data["items"] = [b.to_dict() for b in _filter_items(report, filters, presentation)]
        data["shown"] = len(data["items"])
        return data

    # ------------------------------------------------------------ NL bridge
    def ask(self, text: str, allow_llm_fallback: bool = True, confirmed: bool = False) -> DSLResult:
        """NL → (Layer 1 | 1.5 | 3) → canonical DSL → :meth:`execute`.

        NL-derived commands are never auto-confirmed: actions with ``process``/``write``
        effects need ``confirmed=True`` (CLI ``--yes``, REST/MCP ``confirm: true``).
        """
        trace = uuid.uuid4().hex[:8]
        if not isinstance(text, str) or not text.strip():
            return error_result("VALIDATION_ERROR", "NL_EMPTY_QUERY", "query must be a non-empty string",
                                "nl_fast_path", trace)
        cmd = self.parser.parse(text)
        if cmd is not None:
            layer = "direct_dsl" if cmd.raw_dsl else "nl_fast_path"
            res = self.execute(cmd, source_layer=layer, confirmed=confirmed or layer == "direct_dsl")
            res.meta["nl"] = {"layer": layer, "trace": self.parser.last_trace}
            return res
        negated = self.parser.last_trace.get("rejected") == "negation"
        cached = None if negated else self.cache.match(text)
        if cached:
            res = self.execute(cached[0], source_layer="semantic_cache", confirmed=confirmed)
            res.meta["nl"] = {"layer": "semantic_cache", "confidence": cached[1]}
            return res
        llm_note = None
        if allow_llm_fallback and not negated and self.translator.enabled:
            out = self.translator.translate(text)
            if out and out[0] == "ok":
                res = self.execute(out[1], source_layer="llm_fallback", confirmed=confirmed)
                if res.success:
                    self.cache.learn(text, out[1])
                res.meta["nl"] = {"layer": "llm_fallback"}
                return res
            if out and out[0] == "clarify":
                return DSLResult(False, "CLARIFY", {"clarify": out[1]}, [{
                    "code": "NL_CLARIFY", "message": out[1].get("question") or "please clarify"}],
                    {"executionTimeMs": 0.0, "sourceLayer": "llm_fallback", "canonicalDsl": None, "traceId": trace})
            llm_note = self.translator.last_error
        options = [o.completion_text.strip() for o in self.options.suggest(text, max_suggestions=5)]
        if negated:
            code, msg = "NL_NEGATION_GUARD", "negated request – nothing will be executed (polarity guard)"
        else:
            code, msg = "NL_UNRECOGNIZED_INTENT", f"could not compile '{text}' into a DSL command"
        if llm_note:
            msg += f" (LLM fallback failed: {llm_note})"
        return DSLResult(False, "CLARIFY", {"clarify": {"question": "Which action did you mean?", "options": options}},
                         [{"code": code, "message": msg}],
                         {"executionTimeMs": 0.0, "sourceLayer": "nl_fast_path", "canonicalDsl": None, "traceId": trace})

    def suggest(self, partial: str, max_suggestions: int = 5, context: Optional[Dict[str, Any]] = None
                ) -> Dict[str, Any]:
        return self.options.suggest_envelope(partial, context=context, max_suggestions=max_suggestions)

    # ------------------------------------------------------------ plans
    def run_due(self, now: Optional[int] = None) -> Dict[str, Any]:
        """urtlog-style executor: run due PLANs, cancel misfires, materialise next occurrences."""
        if self.ledger is None:
            raise LedgerNotConfigured("plan execution needs a ledger (--state DIR / BOTTLENECKS_STATE_DIR)")
        t = now or now_us()
        summary: Dict[str, List[Any]] = {"executed": [], "canceled": [], "waiting": [], "scheduled_next": []}
        for plan in self.ledger.pending_plans():
            urt = plan["urt"]
            at, step = urt["at_us"], int(urt.get("step_seconds") or 0)
            window_end = at + int((plan.get("metadata") or {}).get("window_seconds") or 60) * 1_000_000
            canonical = (plan.get("metadata") or {}).get("canonical_dsl", "")
            if t < at:
                summary["waiting"].append({"plan": plan["event_id"], "at": urt["at"]})
                continue
            if t > window_end:
                self.ledger.cancel(plan, "misfire: execution window elapsed", at=t)
                summary["canceled"].append({"plan": plan["event_id"], "reason": "misfire"})
            else:
                res = self.execute(canonical, source_layer="plan_executor", confirmed=True,
                                   causation_id=plan["event_id"])
                if res.meta.get("eventId") is None:  # not recorded (e.g. validation error) – settle explicitly
                    self.ledger.cancel(plan, f"{res.status}: {res.errors[0]['message'] if res.errors else ''}", at=t)
                summary["executed"].append({"plan": plan["event_id"], "status": res.status,
                                            "run": res.meta.get("runId"), "dsl": canonical})
            if step > 0:
                nxt = at + step * 1_000_000
                window = window_end - at
                while nxt + window < t:
                    nxt += step * 1_000_000
                action = self.registry.get((plan.get("metadata") or {}).get("action_id", ""))
                if action is not None:
                    _, _, bound = self.compile(canonical)
                    rec = self.ledger.schedule(action_id=action.id, base_uri=action.uri, arguments=bound,
                                               canonical=canonical, at_us=nxt, step_seconds=step,
                                               window_seconds=window // 1_000_000)
                    summary["scheduled_next"].append({"plan": rec["event_id"], "at": rec["urt"]["at"]})
        return summary


def _filter_items(report: Report, filters: Dict[str, Any], presentation: Dict[str, Any]):
    floor = presentation.get("min_severity") or filters.get("severity")
    items = report.ranked(floor)
    if filters.get("domain"):
        items = [b for b in items if b.domain == filters["domain"]]
    if filters.get("kind"):
        items = [b for b in items if b.kind == filters["kind"]]
    top = presentation.get("top")
    return items[: int(top)] if top else items


__all__ = ["Engine", "PRESENTATION", "FILTERS"]
