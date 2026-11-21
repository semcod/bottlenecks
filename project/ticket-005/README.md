# Ticket 005: Report Linux host pressure for HostGuard

- **ID**: ticket-005
- **Owner**: unresolved:human
- **Status**: IN_PROGRESS
- **Workflow state**: EDIT
- **Created**: 2026-11-21

## Goal and scope

Expose Linux scheduler and pressure-stall counts to monitoring consumers.
This addresses the OneDev host incident where a large Linux load average
represented blocked tasks and raw capacity obscured the space available
to unprivileged services.

SESSION_EXECUTION_AUTHORIZATION: the user requested host resource diagnostics
and improvements to Bottlenecks and FixOS for HostGuard on 2026-11-22.

## Acceptance criteria

- [x] AC-01: The session authorizes the scoped implementation and protected publication.
- [x] AC-02: Linux PSI and scheduler counts distinguish I/O and memory stalls from a runnable backlog.
- [x] AC-03: Disk usage includes actual available capacity and keeps the existing JSON fields.
- [x] AC-04: Missing Linux counters leave the existing non-Linux fallback usable.

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
