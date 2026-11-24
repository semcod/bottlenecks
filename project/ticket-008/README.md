# Ticket 008: Show the tracker and declared project in campaign delivery observations for PLF-25979

- **ID**: ticket-008
- **Owner**: codex
- **Status**: IN_PROGRESS
- **Workflow state**: EDIT
- **Created**: 2026-11-24

## Goal and scope

Report the declared target for cross-repository campaign tickets in Planfile and Monag, using the native GitHub issue binding. Preserve tracker identity for Reflex and omit private titles.

## Acceptance criteria

- [x] AC-01 The same bound ticket in a native Planfile export and Monag snapshot emits the same observation. Ambiguous and unbound records produce no routing claim.
- [x] AC-02 The live tracking store yields per-target counts. Its replay retains event IDs and routes informational observations without a failure code.

## Verification

The native tracker returned 81 tickets from the audited cohort. Its current Monag projection and repeated run agreed on three event IDs for 21 pending tickets. Reflex counted three unique events, zero recurring failures and zero new events on replay. The recovery receipts are stored under PLF-25979.

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
