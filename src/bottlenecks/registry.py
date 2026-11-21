"""Action registry – the single source of truth for every capability.

Follows ``wellmanifest/nl-api-llm`` (one machine-readable registry from which
CLI, REST, MCP tools, autocomplete and NL intents are derived) and
``wellmanifest/nl-uri-dsl-llm`` (every action has a stable Action URI
``proc://bottlenecks/<entity>/<operation>/v1``; results are Resource URNs).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from . import __version__

URI_AUTHORITY = "bottlenecks"
PARAM_TYPES = ("string", "integer", "number", "boolean", "array", "object")
EFFECTS = ("pure", "read", "process", "write", "network")


@dataclass(frozen=True)
class Param:
    name: str
    type: str = "string"
    required: bool = False
    default: Any = None
    description: str = ""
    enum: Tuple[Any, ...] = ()

    def coerce(self, value: Any) -> Any:
        """Coerce a DSL/JSON value into the declared type (raises ValueError)."""
        if value is None:
            return None
        if self.type == "integer":
            if isinstance(value, bool):
                raise ValueError(f"'{self.name}' must be an integer")
            value = int(value)
        elif self.type == "number":
            value = float(value)
        elif self.type == "boolean":
            if isinstance(value, str):
                low = value.strip().lower()
                if low not in ("true", "false", "1", "0", "yes", "no", "tak", "nie"):
                    raise ValueError(f"'{self.name}' must be a boolean")
                value = low in ("true", "1", "yes", "tak")
            else:
                value = bool(value)
        elif self.type == "array":
            if isinstance(value, str):
                value = [v for v in value.split(",") if v] if value else []
            elif not isinstance(value, (list, tuple)):
                raise ValueError(f"'{self.name}' must be an array")
            value = list(value)
        elif self.type == "string":
            value = str(value)
        if self.enum and value not in self.enum:
            raise ValueError(f"'{self.name}' must be one of {list(self.enum)}")
        return value

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"name": self.name, "type": self.type, "location": "arg", "required": self.required}
        if self.default is not None:
            d["default"] = self.default
        if self.description:
            d["description"] = self.description
        if self.enum:
            d["enum"] = list(self.enum)
        return d


@dataclass
class Action:
    entity: str
    operation: str
    title: str
    handler: Callable[..., Any]
    params: Tuple[Param, ...] = ()
    effects: Tuple[str, ...] = ("read",)
    description: str = ""
    produces_report: bool = True
    nl_intents: Dict[str, List[str]] = field(default_factory=dict)
    menu: str = ""

    @property
    def id(self) -> str:
        return f"{self.entity}.{self.operation}"

    @property
    def uri(self) -> str:
        return f"proc://{URI_AUTHORITY}/{self.entity}/{self.operation}/v1"

    @property
    def read_only(self) -> bool:
        return not (set(self.effects) & {"write", "process", "network"})

    @property
    def requires_confirmation(self) -> bool:
        return "process" in self.effects or "write" in self.effects

    def param(self, name: str) -> Optional[Param]:
        return next((p for p in self.params if p.name == name), None)

    def bind(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Validate + coerce arguments; unknown keys are rejected (fail-closed)."""
        unknown = sorted(set(arguments) - {p.name for p in self.params})
        if unknown:
            raise KeyError(", ".join(unknown))
        bound: Dict[str, Any] = {}
        for p in self.params:
            if p.name in arguments and arguments[p.name] not in (None, ""):
                bound[p.name] = p.coerce(arguments[p.name])
            elif p.required:
                raise LookupError(p.name)
            elif p.default is not None:
                bound[p.name] = p.default
        return bound

    def to_endpoint(self) -> Dict[str, Any]:
        """Endpoint entry in the ``nl-api-llm`` registry shape."""
        args = " ".join(f"{p.name}={{{p.name}}}" for p in self.params if p.required)
        command = f"{self.id} {args}".strip()
        return {
            "id": self.id,
            "service": URI_AUTHORITY,
            "protocol": "cli",
            "title": self.title,
            "description": self.description or self.title,
            "readOnly": self.read_only,
            "tags": [self.entity, *self.effects],
            "actionUri": self.uri,
            "effects": list(self.effects),
            "requiresConfirmation": self.requires_confirmation,
            "cli": {"binary": "bottlenecks", "commandTemplate": f'bottlenecks dsl "{command}"'},
            "rest": {"method": "POST", "path": "/dsl"},
            "mcp": {"tool": "execute_dsl"},
            "menu": self.menu or self.entity,
            "parameters": [p.to_dict() for p in self.params],
            "nlIntents": [{"locale": loc, "examples": list(ex)} for loc, ex in sorted(self.nl_intents.items())],
        }


class ActionRegistry:
    def __init__(self, actions: Iterable[Action] = ()) -> None:
        self._actions: Dict[str, Action] = {}
        for a in actions:
            self.register(a)

    def register(self, action: Action) -> Action:
        for e in action.effects:
            if e not in EFFECTS:
                raise ValueError(f"unknown effect '{e}' for {action.id}")
        for p in action.params:
            if p.type not in PARAM_TYPES:
                raise ValueError(f"unknown param type '{p.type}' for {action.id}.{p.name}")
        self._actions[action.id] = action
        return action

    def get(self, action_id: str) -> Optional[Action]:
        return self._actions.get(action_id)

    def by_uri(self, uri: str) -> Optional[Action]:
        base = uri.split("?", 1)[0]
        return next((a for a in self._actions.values() if a.uri == base), None)

    def __iter__(self):
        return iter(sorted(self._actions.values(), key=lambda a: a.id))

    def __len__(self) -> int:
        return len(self._actions)

    def ids(self) -> List[str]:
        return [a.id for a in self]

    def manifest(self) -> Dict[str, Any]:
        return {
            "schema": "wellmanifest.nl-api-llm.api-registry/v1",
            "version": __version__,
            "metadata": {
                "project": "semcod/bottlenecks",
                "adopts": ["wellmanifest/nl-dsl-llm", "wellmanifest/nl-api-llm", "wellmanifest/nl-uri-dsl-llm",
                           "uritime/urtlog record/v2"],
            },
            "services": [{"id": URI_AUTHORITY, "title": "bottlenecks", "protocols": ["cli", "rest", "mcp"],
                          "baseUri": f"proc://{URI_AUTHORITY}/"}],
            "endpoints": [a.to_endpoint() for a in self],
        }
