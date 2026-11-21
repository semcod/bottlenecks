"""Render reports as text, Markdown or JSON."""

from __future__ import annotations

import json
from typing import Optional

from .models import Report

ICONS = {"critical": "!!", "high": "! ", "medium": "~ ", "low": ". ", "info": "  "}


def to_json(report: Report, min_severity: Optional[str] = None, top: Optional[int] = None) -> str:
    data = report.to_dict()
    items = [b.to_dict() for b in report.ranked(min_severity)]
    data["items"] = items[:top] if top else items
    return json.dumps(data, indent=2, default=str)


def to_text(report: Report, min_severity: Optional[str] = None, top: Optional[int] = None) -> str:
    items = report.ranked(min_severity)
    if top:
        items = items[:top]
    lines = [f"bottlenecks: {len(report.items)} finding(s), worst={report.worst_severity()}"]
    for b in items:
        lines.append(f"{ICONS.get(b.severity, '  ')}[{b.severity:<8}] {b.score:.2f} {b.domain}/{b.kind}  {b.location}")
        lines.append(f"      {b.message}")
        if b.suggestion:
            lines.append(f"      -> {b.suggestion}")
    if not items:
        lines.append("  no bottlenecks above threshold")
    return "\n".join(lines)


def to_markdown(report: Report, min_severity: Optional[str] = None, top: Optional[int] = None) -> str:
    items = report.ranked(min_severity)
    if top:
        items = items[:top]
    out = ["# Bottlenecks report", "",
           f"Findings: **{len(report.items)}**, worst severity: **{report.worst_severity()}**", "",
           "| Severity | Score | Domain | Kind | Location | Message | Suggestion |",
           "|---|---:|---|---|---|---|---|"]
    for b in items:
        cells = [b.severity, f"{b.score:.2f}", b.domain, b.kind, f"`{b.location}`", b.message, b.suggestion]
        out.append("| " + " | ".join(c.replace("|", "\\|") for c in cells) + " |")
    return "\n".join(out) + "\n"


RENDERERS = {"text": to_text, "json": to_json, "markdown": to_markdown, "md": to_markdown}


def render(report: Report, fmt: str = "text", min_severity: Optional[str] = None, top: Optional[int] = None) -> str:
    try:
        return RENDERERS[fmt](report, min_severity=min_severity, top=top)
    except KeyError:
        raise ValueError(f"unknown format '{fmt}', choose from {sorted(RENDERERS)}") from None
