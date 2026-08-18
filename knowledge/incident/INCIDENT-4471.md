# INCIDENT-4471: EU_TECH_50 published below expectation

**Date:** 2026-02-11
**Severity:** P2
**Duration:** 14 minutes
**Indices affected:** EU_TECH_50 and two derived strategy indices

## Summary

EU_TECH_50 published values approximately 1.2% below expectation from 09:02 to 09:16 CET. Peer
indices sharing constituents were unaffected, which localised the problem to this index rather than
to the feed or the calculation engine.

## Timeline

- 08:47 — Reference data batch completed. The spin-off corporate action for constituent NTX.AS was
  present in the file but flagged `PENDING_REVIEW` rather than `APPROVED`.
- 09:00 — Market open. NTX.AS opened at the ex-distribution price, correctly reflecting the spin-off.
- 09:02 — First `zscore_5m` detector alert on EU_TECH_50.
- 09:04 — Operations began investigating, initially suspecting a feed problem.
- 09:11 — Peer comparison showed no equivalent deviation, ruling out the feed.
- 09:14 — Divisor history query showed no adjustment applied for the NTX.AS spin-off.
- 09:16 — Divisor manually adjusted; index values recalculated and republished.

## Root cause

The spin-off corporate action was not applied to the divisor because it remained in
`PENDING_REVIEW` status at the calculation cutoff. The constituent's price correctly reflected the
distribution while the divisor did not, so the index absorbed the price drop as if it were a market
move.

The corporate action review queue had a single approver who was unavailable that morning, and there
was no escalation path or cutoff alert.

## Contributing factors

- No alert fires when a corporate action remains unapproved at the calculation cutoff. The failure
  was silent until it manifested as a bad index value.
- The first 9 minutes were spent investigating the feed, because a step change at open resembles a
  feed problem and the peer comparison that distinguishes them was not the first check performed.

## Remediation

1. Alert on any corporate action in `PENDING_REVIEW` within 60 minutes of the calculation cutoff.
   *(Completed 2026-02-18)*
2. Second approver added to the corporate action review rota. *(Completed 2026-02-14)*
3. Peer-index comparison added as the first step in the anomaly triage runbook, ahead of feed
   checks. *(Completed 2026-02-20)*

## Detection signature

A step change at market open affecting one index while peers sharing constituents are unaffected,
with no corresponding divisor history entry, indicates an unprocessed corporate action. This is the
fastest discriminator between a corporate-action failure and a feed or calculation problem.
