# Ticket 009: Exclude filesystem paths from intent and polarity matching in Bottlenecks PLF-001

- **ID**: ticket-009
- **Owner**: codex
- **Status**: IN_PROGRESS
- **Workflow state**: EDIT
- **Created**: 2026-11-24

## Goal and scope

Keep intent, negation and severity independent of tokens in explicit filesystem paths. Preserve literal arguments and path-based action inference.

## Acceptance criteria

- [x] The default governed conformance test passes. A regression exercises path-only inference, explicit actions, literal severity and real negation.

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
