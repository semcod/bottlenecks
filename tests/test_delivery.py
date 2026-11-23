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
