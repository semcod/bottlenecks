"""Read-only analysis of Planfile ticket exports and Monag resume snapshots."""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .models import Bottleneck, Report

_SLUG = re.compile(r"[\w.-]+/[\w.-]+", re.ASCII)
_ID = re.compile(r"[\w.-]{1,96}", re.ASCII)
_CAMPAIGN_TITLE = re.compile(r"^\[(?:Autodo|UR-CQRS|URT)\]\[P[0-9]\]\[([^\]]{1,140})\]\s")
_ISSUE_KEY = re.compile(r"([\w.-]+/[\w.-]+)#[1-9][0-9]*", re.ASCII)
_PENDING = {"open", "blocked", "failed", "in_progress", "review", "waiting_input"}


def _valid_repository(value):
    return isinstance(value, str) and bool(_SLUG.fullmatch(value)) and all(
        part not in {".", ".."} for part in value.split("/")
    )


def _timestamp(value):
    stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("timestamps must include a UTC offset")
    return stamp.astimezone(timezone.utc)


def _campaign_target(row, repo, title):
    match = _CAMPAIGN_TITLE.match(title)
    if not match or not _valid_repository(match[1]):
        return None
    target = match[1].lower()
    if target == repo.lower():
        return None
    # Monag and native Planfile expose the same GitHub issue key in different fields.
    bindings = row.get("github_bindings")
    if bindings is None:
        sync = row.get("sync")
        github = sync.get("github") if isinstance(sync, dict) else None
        bindings = [github.get("key")] if isinstance(github, dict) else []
    if not isinstance(bindings, list) or len(bindings) != 1 or not isinstance(bindings[0], str):
        return None
    issue = _ISSUE_KEY.fullmatch(bindings[0])
    if issue and issue[1].lower() == repo.lower():
        return target
    return None


def _tickets(report, repo, rows, since, min_repeat):
    if not isinstance(rows, list):
        raise ValueError("expected a ticket list")
    groups = defaultdict(set)
    campaign_targets = defaultdict(set)
    ticket_waits = defaultdict(set)
    blocked = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("invalid ticket record")
        ticket_id = row.get("id")
        if not isinstance(ticket_id, str) or not _ID.fullmatch(ticket_id):
            raise ValueError("invalid ticket ID")
        if row.get("status") not in _PENDING:
            ticket_waits[ticket_id].add(None)
            continue
        if since is not None and _timestamp(row.get("created_at")) < since:
            continue
        execution = row.get("execution")
        state = execution.get("state") if isinstance(execution, dict) else None
        blocker = None
        if row["status"] == "open" and not row.get("identity_collision") and isinstance(state, str):
            blocker = _EXECUTION_BLOCKERS.get("exec_state:" + state)
        ticket_waits[ticket_id].add(blocker)
        title = row.get("title") or row.get("name")
        if title and not row.get("identity_collision"):
            signature = " ".join(title.casefold().split())
            groups[signature].add(ticket_id)
            target = _campaign_target(row, repo, title)
            if target:
                campaign_targets[target].add(ticket_id)
        if row["status"] in {"failed", "blocked", "waiting_input"}:
            blocked.add(ticket_id)
    waits = defaultdict(set)
    for ticket_id, reasons in ticket_waits.items():
        if len(reasons) == 1 and None not in reasons:
            waits[next(iter(reasons))].add(ticket_id)
    _queue_findings(report, repo, waits)
    for target, ids in sorted(campaign_targets.items()):
        message = f"Campaign tickets tracked in {repo} name {target} as the implementation repository."
        _finding(report, repo, "campaign-target", ids, message, target=target)
    for signature, ids in sorted(groups.items()):
        if len(ids) >= min_repeat:
            _finding(report, repo, "repeated-ticket-title", ids,
                     "Pending tickets share a title. Compare source records before consolidating.",
                     hashlib.sha256(signature.encode()).hexdigest()[:16])
    if blocked:
        _finding(report, repo, "blocked-tickets", blocked,
                 "Planfile reports blocked or failed attempts or input waits. Inspect their recorded reasons.")


def _finding(report, repo, kind, ids, message, group="", *, target=None):
    location = repo + ("/" + group if group else "")
    examples = sorted(ids)[:8]
    identity = hashlib.sha256(json.dumps([repo, kind, target or group, sorted(ids)]).encode()).hexdigest()
    observation = {"event_type": "delivery.observation", "operation": "delivery.planfile",
                   "repository": target or repo, "source_event_id": identity,
                   "status": "observed" if target or kind == "repeated-ticket-title" else "blocked",
                   "occurrences": len(ids), "summary": message}
    metrics = {"count": len(ids), "examples": examples, "reflex": observation}
    if target:
        metrics.update(tracking_repository=repo, target_repository=target)
        observation["tracking_repository"] = repo
    else:
        scope_digest = hashlib.sha256(repo.encode()).hexdigest()[:16]
        observation["failure_code"] = f"{kind}-{scope_digest}"
    report.items.append(Bottleneck("pipeline", kind, location, message, 0.1 if target else 0.6, metrics=metrics))


_QUEUE_REASONS = {
    "dependency_unresolved": "A declared predecessor is unresolved.",
    "invalid_target_repository": "Planfile could not resolve the target repository.",
    "ambiguous_lease_state": "Planfile could not establish one lease binding.",
    "process_envelope": "The worker process envelope is missing or invalid.",
    "waiting": "A waiting label excludes these tickets.",
    "status_blocked": "These tickets have explicit blocked status.",
    "blocked": "Planfile excluded these tickets because they carry the blocked label.",
    "waiting_dependency": "Execution is waiting for a dependency.",
    "waiting_input": "Execution is waiting for input.",
    "execution_blocked": "Planfile reports that execution is blocked.",
}


_EXECUTION_BLOCKERS = {
    "exec_state:blocked": "execution_blocked",
    "exec_state:waiting_input": "waiting_input",
    "exec_state:waiting_dependency": "waiting_dependency",
}


def _queue_findings(report, repo, groups):
    for kind, ids in sorted(groups.items()):
        examples = ", ".join(sorted(ids)[:8])
        _finding(report, repo, kind, ids, f"{_QUEUE_REASONS[kind]} Affected tickets include {examples}.")


def _queue_report(data):
    repo = data["repository"]
    if not _valid_repository(repo):
        raise ValueError("invalid repository in queue snapshot")
    groups = defaultdict(set)
    for row in data["skipped"]:
        kind = row["category"]
        reason = row.get("reason")
        if kind == "exec_state" and isinstance(reason, str):
            kind = _EXECUTION_BLOCKERS.get(reason, kind)
        if kind in _QUEUE_REASONS:
            ticket = row["id"]
            if not isinstance(ticket, str) or not _ID.fullmatch(ticket):
                raise ValueError("invalid ticket ID in queue snapshot")
            groups[kind].add(ticket)
    report = Report(summary={"projects_analyzed": 1, "scope": "one executable queue snapshot",
                             "candidate_count": len(data["candidates"])})
    _queue_findings(report, repo, groups)
    return report


def analyze_delivery(source, repository=None, since=None, min_repeat=3):
    """Analyze a single native JSON export without following any paths inside it."""
    with Path(source).open(encoding="utf-8") as handle:
        data = json.load(handle)
    if min_repeat < 2:
        raise ValueError("min_repeat must be at least 2")
    report = Report(summary={"projects_analyzed": 0, "projects_incomplete": 0})
    if isinstance(data, list):
        if not _valid_repository(repository):
            raise ValueError("provide repository=owner/name for Planfile ticket list JSON")
        _tickets(report, repository, data, _timestamp(since) if since else None, min_repeat)
        report.summary["projects_analyzed"] = 1
        return report
    if isinstance(data, dict) and data.get("schema") == "planfile.executable-queue/v1":
        if since or repository:
            raise ValueError("queue exports already specify a repository and lack creation times for exclusions")
        return _queue_report(data)
    if not isinstance(data, dict) or data.get("schema") != "monag.resume/v1":
        raise ValueError("expected Planfile ticket list, executable queue or Monag resume JSON")
    if since or repository:
        raise ValueError("repository and since are for Planfile exports only")
    root = Path(data["root"])
    for project in data["projects"]:
        plan = project["planfile"]
        if not plan.get("available"):
            report.summary["projects_incomplete"] += 1
            continue
        path = Path(project["path"])
        repo = path.relative_to(root).as_posix() if path.is_relative_to(root) else None
        if not _valid_repository(repo):
            report.summary["projects_incomplete"] += 1
            continue
        _tickets(report, repo, plan["remaining_tickets"], None, min_repeat)
        report.summary["projects_analyzed"] += 1
        if not plan.get("complete"):
            report.summary["projects_incomplete"] += 1
    return report
