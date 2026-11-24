# Ticket 011: Report explicit Planfile blockers to Reflex (PLF-25982)

- **ID**: ticket-011
- **Owner**: agent:codex
- **Status**: IN_PROGRESS
- **Workflow state**: EDIT
- **Created**: 2026-11-24

## Goal and scope

The user requested future anomaly detection in Bottlenecks, Monag and Reflex. Native Planfile executable-queue reports two evidenced blockers that the delivery DSL currently drops.

## Acceptance criteria

- Project the blocked label and the waiting_dependency execution state to stable Reflex observations.
- Preserve normal queued, archived and leased exclusions. Reproduce both blockers in the original queue snapshots.

## Tracking boundary

This directory contains the minimal reviewed intent. Optional participant prose
and raw command logs are not required delivery output.

<!-- ticket-radar:start -->
## Ticket Radar

Initial planned-scope estimate; advisory only, not execution or merge approval.

| Measure | Initial assessment |
| --- | --- |
| Impact score | 52/100 |
| Complexity | M |
| Estimated effort | 51 min |
| Planning budget | 30 min |
| Split recommended | yes |

Diagnostics: RADAR-ACCEPTANCE-MISSING, RADAR-BUDGET-EXCEEDED.
<!-- ticket-radar:end -->
