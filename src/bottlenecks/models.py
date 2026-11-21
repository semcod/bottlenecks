"""Common data model shared by every analyzer."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional

SEVERITIES = ("info", "low", "medium", "high", "critical")


def severity_rank(severity: str) -> int:
    """Return the ordinal of a severity name (unknown names rank lowest)."""
    try:
        return SEVERITIES.index(severity)
    except ValueError:
        return 0


def severity_from_score(score: float) -> str:
    """Map a normalised score (0..1) to a severity name."""
    if score >= 0.85:
        return "critical"
    if score >= 0.6:
        return "high"
    if score >= 0.35:
        return "medium"
    if score >= 0.15:
        return "low"
    return "info"


@dataclass
class Bottleneck:
    """A single detected bottleneck."""

    domain: str  # code | runtime | pipeline | system
    kind: str  # machine readable rule id, e.g. "io-in-loop"
    location: str  # file:line, function name, task name, resource name
    message: str
    score: float  # normalised impact 0..1
    severity: str = ""
    suggestion: str = ""
    metrics: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.score = max(0.0, min(1.0, float(self.score)))
        if not self.severity:
            self.severity = severity_from_score(self.score)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Report:
    """A ranked collection of bottlenecks plus analyzer summaries."""

    items: List[Bottleneck] = field(default_factory=list)
    summary: Dict[str, Any] = field(default_factory=dict)

    def extend(self, items: Iterable[Bottleneck]) -> "Report":
        self.items.extend(items)
        return self

    def merge(self, other: "Report") -> "Report":
        self.items.extend(other.items)
        self.summary.update(other.summary)
        return self

    def ranked(self, min_severity: Optional[str] = None) -> List[Bottleneck]:
        floor = severity_rank(min_severity) if min_severity else 0
        items = [b for b in self.items if severity_rank(b.severity) >= floor]
        return sorted(items, key=lambda b: (-b.score, b.domain, b.location))

    def top(self, n: int = 10) -> List[Bottleneck]:
        return self.ranked()[:n]

    def worst_severity(self) -> str:
        if not self.items:
            return "info"
        return max((b.severity for b in self.items), key=severity_rank)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "summary": self.summary,
            "count": len(self.items),
            "worst_severity": self.worst_severity(),
            "items": [b.to_dict() for b in self.ranked()],
        }
