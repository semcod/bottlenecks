import json

import pytest

from bottlenecks import Engine


def run(tmp_path, data, suffix=""):
    path = tmp_path / "export.json"
    path.write_text(json.dumps(data))
    return Engine().execute(f"delivery.planfile source={path} {suffix}")


def test_weekly_planfile_scope_and_repeat_identity(tmp_path):
    row = {"id": "A-1", "status": "blocked", "name": "failure with private text",
           "created_at": "2026-11-16T23:00:00Z"}
    rows = [row, row, dict(row, id="A-2"), dict(row, id="A-3", status="done"),
            dict(row, id="A-4", created_at="2026-11-16T22:59:59Z")]
    opts = "repository=org/repo since=2026-11-17T00:00:00+01:00 min_repeat=2"
    result = run(tmp_path, rows, opts)
    assert result.success
    findings = {item.kind: item.metrics for item in result.report.items}
    assert set(findings) == {"blocked-tickets", "repeated-ticket-title"}
    assert findings["blocked-tickets"]["examples"] == ["A-1", "A-2"]
    assert findings["repeated-ticket-title"]["count"] == 2
    assert "private text" not in json.dumps(result.data)
    assert run(tmp_path, list(reversed(rows)), opts).data == result.data


def test_monag_repository_isolation_and_coverage(tmp_path):
    ticket = {"id": "T-1", "title": "repeated failure", "status": "blocked"}
    project = {"path": "/work/org/one", "planfile": {"available": True, "complete": True,
               "remaining_tickets": [ticket]}}
    data = {"schema": "monag.resume/v1", "root": "/work", "projects": [
        project, dict(project, path="/work/org/two"), {"planfile": {"available": False}}]}
    result = run(tmp_path, data)
    assert result.success
    assert result.report.summary == {"projects_analyzed": 2, "projects_incomplete": 1}
    assert {item.metrics["reflex"]["repository"] for item in result.report.items} == {"org/one", "org/two"}
    assert {item.kind for item in result.report.items} == {"blocked-tickets"}
    assert len({item.metrics["reflex"]["failure_code"] for item in result.report.items}) == 2


@pytest.mark.parametrize("data,opts", [([], ""), ({"schema": "bad"}, ""),
                         ([{"id": "A", "status": "blocked"}], "repository=org/repo since=2026-11-17")])
def test_invalid_exports_fail_visibly(tmp_path, data, opts):
    assert not run(tmp_path, data, opts).success


def test_canonical_queue_only_reports_evidenced_blockers(tmp_path):
    data = {"schema": "planfile.executable-queue/v1", "repository": "org/repo",
            "candidates": [{"id": "T-1"}], "skipped": [
                {"id": "T-2", "category": "active_lease"},
                {"id": "T-3", "category": "archived"},
                {"id": "T-4", "category": "dependency_unresolved"}]}
    result = run(tmp_path, data)
    assert result.success
    assert result.report.summary["candidate_count"] == 1
    finding, = result.report.items
    assert finding.kind == "dependency_unresolved"
    assert finding.metrics["examples"] == ["T-4"]


def test_queue_emits_reflex_observations_only_for_explicit_blockers(tmp_path):
    skipped = [
        {"id": "T-1", "category": "blocked", "reason": "label:blocked"},
        {"id": "T-2", "category": "exec_state", "reason": "exec_state:waiting_dependency"},
        {"id": "T-3", "category": "exec_state", "reason": "exec_state:queued"},
        {"id": "T-4", "category": "exec_state"},
        {"id": "T-5", "category": "exec_state", "reason": "exec_state:waiting_input"},
        {"id": "T-6", "category": "exec_state", "reason": "exec_state:blocked"},
        {"id": "T-7", "category": "exec_state", "reason": "exec_state:running"},
        {"id": "T-8", "category": "exec_state", "reason": "exec_state:done"},
        {"id": "T-9", "category": "exec_state", "reason": "exec_state:other"},
        {"id": "T-10", "category": "exec_state", "reason": []},
    ]
    data = {"schema": "planfile.executable-queue/v1", "repository": "org/repo",
            "candidates": [], "skipped": skipped}
    result = run(tmp_path, data)
    assert result.success
    found = {item.kind: item.metrics for item in result.report.items}
    assert {kind: m["examples"] for kind, m in found.items()} == {
        "blocked": ["T-1"], "waiting_dependency": ["T-2"],
        "waiting_input": ["T-5"], "execution_blocked": ["T-6"],
    }
    for metrics in found.values():
        assert metrics["reflex"]["status"] == "blocked"
        assert metrics["examples"][0] in metrics["reflex"]["summary"]
    assert run(tmp_path, dict(data, skipped=list(reversed(skipped)))).data == result.data


def test_campaign_route_uses_bound_tracker_and_declared_target(tmp_path):
    title = "[Autodo][P0][dev/worker] private issue contents"
    ticket = {"id": "GITHUB-34", "name": title, "status": "open", "sync": {"github": {"key": "dev/tracker#34"}}}
    result = run(tmp_path, [ticket, ticket], "repository=dev/tracker")
    routing, = [item for item in result.report.items if item.kind == "campaign-target"]
    assert routing.metrics["count"] == 1
    assert routing.metrics["tracking_repository"] == "dev/tracker"
    assert routing.metrics["target_repository"] == "dev/worker"
    observation = routing.metrics["reflex"]
    assert observation["repository"] == "dev/worker"
    assert observation["status"] == "observed"
    assert "failure_code" not in observation
    assert "private issue contents" not in json.dumps(observation)
    row = {"id": "GITHUB-34", "title": title, "status": "open", "github_bindings": ["dev/tracker#34"]}
    project = {"path": "/work/dev/tracker", "planfile": {"available": True, "complete": True,
               "remaining_tickets": [row, dict(row, id="X", identity_collision=True),
                                     dict(row, id="Y", github_bindings=["dev/tracker#34", "dev/other#9"]),
                                     dict(row, id="U", github_bindings=[]),
                                     dict(row, id="Z", github_bindings=["dev/other#34"]),
                                     dict(row, id="M", title="[URT][P1][../bad] hidden"),
                                     dict(row, id="Q", status="done"),
                                     dict(row, id="R", title="[Autodo][P0][dev/tracker] own ticket")]}}
    snapshot = {"schema": "monag.resume/v1", "root": "/work", "projects": [project]}
    result = run(tmp_path, snapshot)
    route, = [item for item in result.report.items if item.kind == "campaign-target"]
    assert route.metrics["reflex"] == observation
    assert route.metrics["examples"] == ["GITHUB-34"]
