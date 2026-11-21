"""Layer 0.5 – real-time autocomplete / Option Network (``nl-dsl-llm`` §2.5).

Suggestions are derived from the action registry (DSL prefixes) and the NL
lexicon examples, and bound to live context ("digital twin"): existing source
directories, pipeline specs and urtlog state dirs in the working directory.
Non-existent resources are never suggested; actions with ``process``/``write``
effects are flagged ``requires_confirmation``.
"""

from __future__ import annotations

import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .nl import fold, load_lexicon
from .registry import ActionRegistry

_SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "build", "dist", ".tox", ".mypy_cache"}


@dataclass
class OptionNode:
    id: str
    display_label: str
    completion_text: str
    action_id: str
    resolved_uri: str
    bound_arguments: Dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0
    requires_confirmation: bool = False
    badge: str = ""
    next_options: List["OptionNode"] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def discover_context(root: str = ".", limit: int = 8) -> Dict[str, List[str]]:
    """Cheap one-level scan of ``root`` for bindable resources."""
    ctx: Dict[str, List[str]] = {"source_dirs": [], "pipeline_specs": [], "event_logs": [], "scripts": []}
    try:
        entries = sorted(os.scandir(root), key=lambda e: e.name)
    except OSError:
        return ctx
    for e in entries:
        name = e.name
        path = name if root in (".", "") else os.path.join(root, name)
        try:
            if e.is_dir() and name not in _SKIP_DIRS and not name.startswith("."):
                if os.path.exists(os.path.join(path, "events.jsonl")):
                    ctx["event_logs"].append(path)
                elif any(f.endswith(".py") for f in os.listdir(path)[:200]) or name in ("src", "lib", "app"):
                    ctx["source_dirs"].append(path)
            elif e.is_dir() and name in (".bottlenecks", ".urtlog") and os.path.exists(os.path.join(path, "events.jsonl")):
                ctx["event_logs"].append(path)
            elif name.endswith(".json") and ("pipeline" in name or "dag" in name):
                ctx["pipeline_specs"].append(path)
            elif name.endswith(".jsonl"):
                ctx["event_logs"].append(path)
            elif name.endswith(".py"):
                ctx["scripts"].append(path)
        except OSError:
            continue
    return {k: v[:limit] for k, v in ctx.items()}


_BINDINGS = {  # action -> (argument, context key)
    "code.scan": ("path", "source_dirs"),
    "scan.all": ("path", "source_dirs"),
    "pipeline.analyze": ("spec", "pipeline_specs"),
    "pipeline.urtlog": ("source", "event_logs"),
    "runtime.profile": ("script", "scripts"),
}


class OptionNetwork:
    def __init__(self, registry: ActionRegistry, lexicon: Optional[Dict[str, Any]] = None) -> None:
        self.registry = registry
        lexicon = lexicon or load_lexicon()
        self.examples: Dict[str, List[str]] = {}
        for item in lexicon.get("intents", ()):
            ex = [e for loc in (item.get("examples") or {}).values() for e in loc]
            self.examples[item["action"]] = ex
        self._ctx_cache: Optional[Dict[str, List[str]]] = None

    def _context(self, context: Optional[Dict[str, Any]]) -> Dict[str, List[str]]:
        if context is not None:
            return {k: list(v) for k, v in context.items() if isinstance(v, (list, tuple))}
        if self._ctx_cache is None:
            self._ctx_cache = discover_context()
        return self._ctx_cache

    def _score(self, action, q: str) -> Tuple[float, str]:
        if action.id.startswith(q) or q.startswith(action.id):
            return 1.0, action.id
        best: Tuple[float, str] = (0.0, "")
        for ex in self.examples.get(action.id, ()):
            fex = fold(ex)
            if fex.startswith(q):
                return 0.9, ex
            if len(q) >= 3 and best[0] < 0.7 and any(w.startswith(q) for w in fex.split()):
                best = (0.7, ex)
        if not best[0] and len(q) >= 3 and (q in action.entity or q in fold(action.title)):
            best = (0.6, action.title)
        return best

    @staticmethod
    def _children(action, score: float, ctx: Dict[str, List[str]], badge: str) -> List[OptionNode]:
        def child(ident: str, label: str, completion: str, bound: Dict[str, Any], weight: float) -> OptionNode:
            return OptionNode(id=ident, display_label=label, completion_text=completion, action_id=action.id,
                              resolved_uri=action.uri, bound_arguments=bound, confidence=round(score * weight, 2),
                              requires_confirmation=action.requires_confirmation, badge=badge)

        children = []
        binding = _BINDINGS.get(action.id)
        if binding:
            arg, key = binding
            children += [child(f"{action.id}:{v}", f"{arg}={v}", f"{action.id} {arg}={v}", {arg: v}, 0.95)
                         for v in ctx.get(key, [])[:5]]
        if action.produces_report:
            children += [child(f"{action.id}:severity={lvl}", f"only {lvl}+", f"{action.id} min_severity={lvl}",
                               {"min_severity": lvl}, 0.9) for lvl in ("critical", "high")]
        return children

    def suggest(self, partial: str, context: Optional[Dict[str, Any]] = None,
                max_suggestions: int = 5) -> List[OptionNode]:
        if not isinstance(partial, str) or max_suggestions <= 0:
            return []
        q = fold(partial.strip())
        if not q:
            return []
        ctx = self._context(context)
        scored = [(s, a, label) for a in self.registry for s, label in [self._score(a, q)] if s]
        scored.sort(key=lambda t: (-t[0], t[1].id))
        out: List[OptionNode] = []
        for score, action, label in scored[:max_suggestions]:
            badge = "EXEC" if "process" in action.effects else ("WRITE" if "write" in action.effects else "")
            out.append(OptionNode(
                id=action.id, display_label=label if label == action.title else f"{action.title}  ·  {label}",
                completion_text=action.id + " ", action_id=action.id, resolved_uri=action.uri,
                confidence=round(score, 2), requires_confirmation=action.requires_confirmation, badge=badge,
                next_options=self._children(action, score, ctx, badge)))
        return out

    def suggest_envelope(self, partial: str, context: Optional[Dict[str, Any]] = None,
                         max_suggestions: int = 5) -> Dict[str, Any]:
        t0 = time.perf_counter()
        options = self.suggest(partial, context, max_suggestions)
        return {"success": True, "partial_query": partial, "suggestions": [o.to_dict() for o in options],
                "latency_ms": round((time.perf_counter() - t0) * 1000, 3)}
