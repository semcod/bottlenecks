import pytest

from bottlenecks.pipeline import analyze_events


OP = "op/check/reflex/ops.onedev-status-publication/github-name-resolution"


def fact(transition, **overrides):
    opening = transition == "opened"
    return {
        "kind": "log", "source": "reflex-check", "repository": "semcod/bottlenecks",
        "owner_principal": "ops", "uri": {"op_path": OP}, "urt": {"kind": "instant"},
        "event_id": "fact-first" if opening else "fact-next", "causation_id": None if opening else "fact-first",
        "status": "failed" if opening else "succeeded",
        "metadata": {"transition": transition, "target": "pr-9", "episode": "fact-first",
                     "failure_category": "transport-unavailable"},
        **overrides,
    }


def test_first_observation_and_replay_need_no_duration_or_repeat():
    opened = fact("opened")
    report = analyze_events([opened, opened, fact("repeat", status="failed")])
    issue, = report.items
    assert issue.kind == "check-without-observed-recovery" and issue.location == OP
    assert issue.metrics["episode"] == "fact-first"
    assert issue.metrics["failure_category"] == "transport-unavailable"
    assert report.summary["check_episodes"] == {"opened": 1, "recovered": 0, "without_observed_recovery": 1}
    assert report.summary["duration_coverage"]["measured"] == 0


def test_verified_recovery_works_in_an_unordered_window():
    report = analyze_events([fact("recovered"), fact("opened"), fact("recovered")])
    assert report.items == []
    assert report.summary["check_episodes"]["recovered"] == 1
    assert analyze_events([fact("recovered")]).items == []
    assert analyze_events([fact("opened")], op_prefix="op/another/").items == []


@pytest.mark.parametrize("changes", [
    {"source": "another-producer"}, {"repository": "other/repo"}, {"owner_principal": "another"},
    {"causation_id": "another-episode"}, {"status": "failed"}, {"metadata": None},
])
def test_unrelated_or_malformed_recovery_cannot_hide_failure(changes):
    assert len(analyze_events([fact("opened"), fact("recovered", **changes)]).items) == 1
