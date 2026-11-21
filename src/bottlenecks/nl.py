"""Natural-language intake (``wellmanifest/nl-dsl-llm`` Layers 1, 1.5 and 3).

NL is an **intake boundary, never an executor**: every layer only produces a
:class:`~bottlenecks.dsl.DSLCommand`; the engine validates it against the action
registry before anything runs.

* Layer 1   – :class:`IntentParser`: deterministic PL/EN lexicon + slot extraction (<1 ms, 0 tokens)
* Layer 1.5 – :class:`SemanticCache`: recurring intents with polarity and slot guards
* Layer 3   – :class:`LLMTranslator`: optional, provider-agnostic, JSON-schema constrained
  fallback compiler (OpenAI-compatible ``/chat/completions``); disabled unless configured.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .dsl import DSLCommand
from .registry import ActionRegistry

_PROFILE = Path(__file__).with_name("profiles") / "nl-lexicon.json"
_FOLD = str.maketrans({"ł": "l", "Ł": "L", "ø": "o", "đ": "d", "ß": "ss"})
_PATH_EXT = (".py", ".json", ".jsonl", ".prof", ".pstats")
_DSL_HEAD_RE = re.compile(r"^[a-z0-9_-]+\.[a-z0-9_-]+(?:\s|$)", re.IGNORECASE)


def fold(text: str) -> str:
    """Lower-case + strip diacritics (``ąćęłńóśźż`` → ascii)."""
    text = text.translate(_FOLD)
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)).lower()


def load_lexicon(path: Optional[os.PathLike] = None) -> Dict[str, Any]:
    return json.loads(Path(path or _PROFILE).read_text(encoding="utf-8"))


class _Stems:
    """Compiled stem matcher: prefix, exact (``word$``) and phrase (``two words``) stems.

    With ``fuzzy=True`` a word also matches a prefix stem (>= 5 chars) within
    Levenshtein distance 1 – typo tolerance as in ``wellmanifest/nl-uri-dsl-llm`` §2.1.
    """

    def __init__(self, stems: Sequence[str], fuzzy: bool = False) -> None:
        self.prefix, self.exact, self.phrase = [], set(), []
        self.fuzzy = fuzzy
        for raw in stems:
            s = fold(raw)
            if " " in s:
                self.phrase.append(s)
            elif s.endswith("$"):
                self.exact.add(s[:-1])
            else:
                self.prefix.append(s)

    def matches(self, words: Sequence[str], text: str) -> List[str]:
        hit = [p for p in self.phrase if p in text]
        for w in words:
            if w in self.exact:
                hit.append(w)
            elif any(w.startswith(p) for p in self.prefix):
                hit.append(w)
            elif self.fuzzy and len(w) >= 5 and any(
                    len(p) >= 5 and _levenshtein(w[:len(p)], p) <= 1 for p in self.prefix):
                hit.append(w)
        return hit


def _levenshtein(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _words(folded: str) -> List[str]:
    return re.findall(r"[a-z0-9']+", folded)


@dataclass
class Slots:
    paths: List[str] = field(default_factory=list)
    severity: Optional[str] = None
    top: Optional[int] = None
    every_seconds: Optional[int] = None
    interval: Optional[float] = None
    schedule: bool = False

    def signature(self) -> Tuple[str, ...]:
        kinds = sorted({_path_kind(p) for p in self.paths})
        return tuple(kinds + (["severity"] if self.severity else []) + (["top"] if self.top else [])
                     + (["every"] if self.every_seconds else []))


def _path_kind(path: str) -> str:
    low = path.lower()
    for ext in _PATH_EXT:
        if low.endswith(ext):
            return ext[1:]
    if os.path.isdir(path) and os.path.exists(os.path.join(path, "events.jsonl")):
        return "events"
    return "dir"


class IntentParser:
    """Layer 1: deterministic multilingual (PL + EN) fast path."""

    def __init__(self, registry: ActionRegistry, lexicon: Optional[Dict[str, Any]] = None) -> None:
        self.registry = registry
        self.lexicon = lexicon or load_lexicon()
        self.negation = _Stems(self.lexicon.get("negation", ()))
        self.generic = _Stems(self.lexicon.get("generic", ()))
        self.schedule = _Stems(self.lexicon.get("schedule", ()))
        self.severity = {k: _Stems(v) for k, v in self.lexicon.get("severity", {}).items()}
        self.units = {k: _Stems(v) for k, v in self.lexicon.get("units", {}).items()}
        self.intents = []
        for item in self.lexicon.get("intents", ()):
            if registry.get(item["action"]) is None:
                continue
            stems = [s for loc in item.get("stems", {}).values() for s in loc]
            self.intents.append((item["action"], int(item.get("priority", 0)), _Stems(stems, fuzzy=True)))
        self.last_trace: Dict[str, Any] = {}

    # slots --------------------------------------------------------------
    def is_negated(self, text: str) -> bool:
        folded = fold(text)
        return bool(self.negation.matches(_words(folded), folded))

    def extract_slots(self, text: str, stem_words: Sequence[str] = ()) -> Slots:
        folded = fold(text)
        slots = Slots(paths=_extract_paths(text, stem_words), severity=self._severity(folded),
                      top=_extract_top(folded), interval=_extract_interval(folded))
        slots.every_seconds = self._every(folded)
        slots.schedule = bool(slots.every_seconds) or bool(
            [w for w in self.schedule.matches(_words(folded), folded) if w not in ("co", "every")])
        return slots

    def _severity(self, folded: str) -> Optional[str]:
        text = re.sub(r"sciez\w*\s+krytyczn\w*|krytyczn\w*\s+sciez\w*|critical\s+path", " ", folded)
        words = _words(text)
        return next((lvl for lvl in ("critical", "high", "medium", "low")
                     if lvl in self.severity and self.severity[lvl].matches(words, text)), None)

    def _every(self, folded: str) -> Optional[int]:
        m = re.search(r"\b(?:co|every)\s+(\d+)?\s*([a-z]+)", folded)
        if not m:
            return None
        unit_word = m.group(2)
        mult = next((mult for unit, mult in (("s", 1), ("m", 60), ("h", 3600), ("d", 86400))
                     if unit in self.units and self.units[unit].matches([unit_word], unit_word)), None)
        return int(m.group(1) or 1) * mult if mult else None

    # intent -------------------------------------------------------------
    def _candidates(self, words: List[str], folded: str) -> List[Tuple[int, int, str, List[str]]]:
        scored = []
        for action_id, priority, stems in self.intents:
            hits = stems.matches(words, folded)
            if hits:
                scored.append((len(hits), priority, action_id, hits))
        return sorted(scored, key=lambda t: (-t[0], -t[1]))

    def _build(self, action_id: str, slots: Slots) -> Tuple[Dict[str, Any], List[str]]:
        """Bind slots to the action's parameters; return (arguments, missing_required)."""
        action = self.registry.get(action_id)
        args = _bind_paths(action_id, slots.paths)
        if action_id in ("system.sample", "scan.all") and slots.interval:
            args["interval"] = slots.interval
        if action is not None and action.produces_report:
            args.update({k: v for k, v in (("min_severity", slots.severity), ("top", slots.top)) if v})
        missing = [p.name for p in (action.params if action else ()) if p.required and p.name not in args]
        return args, missing

    def _order(self, candidates, slots: Slots, words: List[str], folded: str) -> List[str]:
        if slots.schedule and slots.every_seconds:
            candidates = [c for c in candidates if not c[2].startswith(("plan.", "history.", "registry."))]
        order = [c[2] for c in candidates]
        inferred = _infer_from_paths(slots.paths)
        if inferred and inferred not in order:
            order.append(inferred)
        if not order and self.generic.matches(words, folded) and self.registry.get("scan.all"):
            order = ["scan.all"]
        return order

    def _choose(self, order: List[str], slots: Slots) -> Tuple[str, Dict[str, Any], List[str]]:
        """First candidate whose required parameters can be filled; else the best one (reports what is missing)."""
        for action_id in order:
            args, missing = self._build(action_id, slots)
            if not missing:
                return action_id, args, missing
        args, missing = self._build(order[0], slots)
        return order[0], args, missing

    def parse(self, text: str) -> Optional[DSLCommand]:
        self.last_trace = {}
        if not isinstance(text, str) or not text.strip():
            return None
        clean = text.strip()
        direct = self._direct_dsl(clean)
        if direct is not None:
            return direct
        folded = fold(clean)
        words = _words(folded)
        if self.negation.matches(words, folded):
            self.last_trace = {"rejected": "negation"}
            return None
        candidates = self._candidates(words, folded)
        slots = self.extract_slots(clean, {w for c in candidates for w in c[3]})
        order = self._order(candidates, slots, words, folded)
        if not order:
            return None
        chosen, args, missing = self._choose(order, slots)
        entity, operation = chosen.split(".", 1)
        cmd = DSLCommand(entity, operation, args)
        if slots.schedule and slots.every_seconds and not chosen.startswith("plan."):
            cmd = DSLCommand("plan", "schedule", {"command": cmd.to_canonical_string(),
                                                   "every": f"PT{slots.every_seconds}S"})
        self.last_trace = {"candidates": [c[2] for c in candidates], "chosen": chosen, "missing": missing,
                           "slots": slots.__dict__}
        return cmd

    def _direct_dsl(self, clean: str) -> Optional[DSLCommand]:
        if not _DSL_HEAD_RE.match(clean):
            return None
        try:
            cmd = DSLCommand.from_text(clean)
        except ValueError:
            return None
        return cmd if self.registry.get(cmd.action_id) else None


# ------------------------------------------------------------- slot helpers
_PATH_BINDINGS = {  # action -> (argument, accepted path kinds in priority order (None = any), multiple)
    "code.scan": ("path", ("py", "dir"), True),
    "scan.all": ("path", None, True),
    "runtime.profile": ("script", ("py",), False),
    "runtime.pstats": ("file", ("prof", "pstats"), False),
    "pipeline.analyze": ("spec", ("json",), False),
    "pipeline.urtlog": ("source", ("jsonl", "events", "dir"), False),
}
_INFER_ORDER = (("prof", "runtime.pstats"), ("pstats", "runtime.pstats"), ("jsonl", "pipeline.urtlog"),
                ("events", "pipeline.urtlog"), ("json", "pipeline.analyze"), ("py", "code.scan"), ("dir", "code.scan"))


def _bind_paths(action_id: str, paths: Sequence[str]) -> Dict[str, Any]:
    spec = _PATH_BINDINGS.get(action_id)
    if not spec or not paths:
        return {}
    arg, kinds, multiple = spec
    if kinds is None:
        picked = list(paths)
    else:
        picked = sorted((p for p in paths if _path_kind(p) in kinds), key=lambda p: kinds.index(_path_kind(p)))
    if not picked and action_id == "code.scan":
        picked = list(paths)
    if not picked:
        return {}
    return {arg: picked if multiple else picked[0]}


def _infer_from_paths(paths: Sequence[str]) -> Optional[str]:
    kinds = {_path_kind(p) for p in paths}
    return next((action for kind, action in _INFER_ORDER if kind in kinds), None)


def _extract_paths(text: str, stem_words: Sequence[str]) -> List[str]:
    paths = []
    for raw in re.findall(r"[\w./~:\\-]+", text):
        token = raw if raw in (".", "..") else raw.rstrip(".,;:!?")
        if not token or token.lower() in ("i/o", "w/o"):
            continue
        syntactic = "/" in token or token.startswith((".", "~")) or token.lower().endswith(_PATH_EXT)
        on_disk = (fold(token) not in stem_words and len(token) > 1 and not token.isdigit()
                   and os.path.exists(token))
        if syntactic or on_disk:
            paths.append(os.path.expanduser(token))
    return paths


def _extract_top(folded: str) -> Optional[int]:
    m = re.search(r"\btop\s*-?\s*(\d{1,3})\b", folded) or re.search(
        r"\b(\d{1,3})\s+(?:najgorsz|najwieksz|najwazniejsz|pierwsz|worst|biggest|largest|top)", folded)
    return int(m.group(1)) if m else None


def _extract_interval(folded: str) -> Optional[float]:
    m = re.search(r"\b(?:przez|for)\s+(\d+(?:[.,]\d+)?)\s*(?:s\b|sek|sec)", folded)
    return float(m.group(1).replace(",", ".")) if m else None


# ---------------------------------------------------------------- Layer 1.5
@dataclass
class CachedIntent:
    pattern: str
    tokens: Tuple[str, ...]
    command: Dict[str, Any]
    signature: Tuple[str, ...]
    negated: bool = False


class SemanticCache:
    """Reuse of recurring intents with explicit polarity and slot-signature guards (fail-closed)."""

    def __init__(self, parser: IntentParser, threshold: float = 0.75, path: Optional[os.PathLike] = None) -> None:
        self.parser = parser
        self.threshold = threshold
        self.path = Path(path) if path else None
        self.entries: List[CachedIntent] = []
        if self.path and self.path.exists():
            try:
                for e in json.loads(self.path.read_text(encoding="utf-8")):
                    self.entries.append(CachedIntent(e["pattern"], tuple(e["tokens"]), e["command"],
                                                     tuple(e["signature"]), bool(e.get("negated"))))
            except (ValueError, KeyError, TypeError):
                self.entries = []

    def _normalise(self, text: str) -> Tuple[Tuple[str, ...], Slots, bool]:
        slots = self.parser.extract_slots(text)
        folded = fold(text)
        for p in slots.paths:
            folded = folded.replace(fold(p), " ")
        folded = re.sub(r"\d+", " <n> ", folded)
        return tuple(sorted(set(_words(folded)))), slots, self.parser.is_negated(text)

    def learn(self, text: str, command: DSLCommand) -> None:
        tokens, slots, negated = self._normalise(text)
        template = command.to_dict()
        template["arguments"] = {k: ("<path>" if (isinstance(v, str) and v in slots.paths) or
                                     (isinstance(v, list) and v and all(x in slots.paths for x in v)) else v)
                                 for k, v in command.arguments.items()}
        self.entries = [e for e in self.entries if e.tokens != tokens]
        self.entries.append(CachedIntent(text.strip(), tokens, template, slots.signature(), negated))
        self._save()

    def match(self, text: str) -> Optional[Tuple[DSLCommand, float]]:
        if not isinstance(text, str) or not text.strip():
            return None
        tokens, slots, negated = self._normalise(text)
        best: Optional[Tuple[CachedIntent, float]] = None
        for e in self.entries:
            if e.negated != negated or e.signature != slots.signature():
                continue
            union = set(tokens) | set(e.tokens)
            score = len(set(tokens) & set(e.tokens)) / len(union) if union else 0.0
            if score >= self.threshold and (best is None or score > best[1]):
                best = (e, score)
        if best is None:
            return None
        data = json.loads(json.dumps(best[0].command))
        for k, v in list(data["arguments"].items()):
            if v == "<path>":
                data["arguments"][k] = slots.paths if len(slots.paths) != 1 or k in ("path",) else slots.paths[0]
        for key, val in (("top", slots.top), ("min_severity", slots.severity)):
            if key in data["arguments"] and val:
                data["arguments"][key] = val
        return DSLCommand.from_dict(data), round(best[1], 3)

    def _save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps([e.__dict__ for e in self.entries], ensure_ascii=False, indent=1),
                             encoding="utf-8")


# ------------------------------------------------------------------ Layer 3
Completion = Callable[[List[Dict[str, str]], Dict[str, Any]], str]


class LLMTranslator:
    """Schema-constrained LLM fallback: NL → ``{"status","action","arguments"}`` → DSLCommand.

    Configure with ``BOTTLENECKS_LLM_URL`` (OpenAI-compatible base URL, e.g.
    ``http://localhost:11434/v1`` or ``https://openrouter.ai/api/v1``),
    ``BOTTLENECKS_LLM_MODEL`` and optionally ``BOTTLENECKS_LLM_API_KEY``.
    Or inject ``complete=callable(messages, schema) -> str`` (tests, other SDKs).
    """

    def __init__(self, registry: ActionRegistry, complete: Optional[Completion] = None, url: Optional[str] = None,
                 model: Optional[str] = None, api_key: Optional[str] = None, timeout: float = 30.0) -> None:
        self.registry = registry
        self.url, self.model, self.api_key, self.timeout = url, model, api_key, timeout
        self._complete = complete
        self.last_error: Optional[str] = None

    @classmethod
    def from_env(cls, registry: ActionRegistry) -> "LLMTranslator":
        return cls(registry, url=os.environ.get("BOTTLENECKS_LLM_URL") or None,
                   model=os.environ.get("BOTTLENECKS_LLM_MODEL") or None,
                   api_key=os.environ.get("BOTTLENECKS_LLM_API_KEY") or None)

    @property
    def enabled(self) -> bool:
        return self._complete is not None or bool(self.url and self.model)

    def schema(self) -> Dict[str, Any]:
        return {
            "type": "object", "additionalProperties": False, "required": ["status"],
            "properties": {
                "status": {"type": "string", "enum": ["ok", "clarify"]},
                "action": {"type": "string", "enum": self.registry.ids()},
                "arguments": {"type": "object"},
                "question": {"type": "string"},
                "options": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
            },
        }

    def _messages(self, text: str) -> List[Dict[str, str]]:
        lines = []
        for a in self.registry:
            params = ", ".join(f"{p.name}:{p.type}{'*' if p.required else ''}" for p in a.params)
            lines.append(f"- {a.id}({params}) – {a.title}")
        system = ("You compile a user request (Polish or English) about performance bottlenecks into ONE action of "
                  "the bottlenecks tool. Reply with JSON only, matching the schema. Use only these actions "
                  "(* = required):\n" + "\n".join(lines) +
                  "\nIf the request is ambiguous or no action fits, reply {\"status\":\"clarify\",\"question\":...,"
                  "\"options\":[canonical DSL strings]}. Never invent actions or parameters.")
        return [{"role": "system", "content": system}, {"role": "user", "content": text}]

    def _http_complete(self, messages: List[Dict[str, str]], schema: Dict[str, Any]) -> str:
        body = {"model": self.model, "messages": messages, "temperature": 0, "max_tokens": 300,
                "response_format": {"type": "json_schema",
                                    "json_schema": {"name": "bottlenecks_dsl", "schema": schema}}}
        req = urllib.request.Request(self.url.rstrip("/") + "/chat/completions",  # type: ignore[union-attr]
                                     data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json",
                                              **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {})})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 - user-configured endpoint
            payload = json.loads(resp.read().decode("utf-8"))
        return payload["choices"][0]["message"]["content"]

    def translate(self, text: str) -> Optional[Tuple[str, Any]]:
        """Return ``("ok", DSLCommand)``, ``("clarify", {...})`` or ``None`` (fail-closed)."""
        self.last_error = None
        if not self.enabled or not isinstance(text, str) or not text.strip():
            return None
        try:
            complete = self._complete or self._http_complete
            raw = complete(self._messages(text.strip()), self.schema()).strip()
            raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
            data = json.loads(raw)
            if data.get("status") == "clarify":
                return "clarify", {"question": str(data.get("question", "")),
                                   "options": [str(o) for o in (data.get("options") or [])][:5]}
            action = self.registry.get(str(data.get("action", "")))
            if action is None:
                self.last_error = f"LLM proposed unknown action {data.get('action')!r}"
                return None
            args = data.get("arguments") or {}
            if not isinstance(args, dict):
                self.last_error = "LLM arguments must be an object"
                return None
            return "ok", DSLCommand(action.entity, action.operation, args)
        except Exception as exc:  # noqa: BLE001 - any provider failure falls back to clarify
            self.last_error = f"{type(exc).__name__}: {exc}"
            return None
