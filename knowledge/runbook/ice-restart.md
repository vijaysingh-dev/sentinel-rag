# ICE Restart Procedure

The index calculation engine (ICE) computes real-time index values from constituent prices. A
restart during market hours risks a publication gap, so the drain-first sequence below is
mandatory rather than advisory.

## When to restart

- Calculation lag exceeds 5 seconds sustained for more than 2 minutes
- Divisor cache is confirmed stale after a corporate action
- A configuration change requires a reload and hot reload has failed

Do **not** restart in response to a single anomalous index value. Investigate first — a restart
clears the in-memory state that would tell you what went wrong.

## Steps

1. Confirm the calculation lag in the system monitor health tree. If lag is normal, the problem is
   downstream and a restart will not help.
2. Announce the maintenance window in the operations channel. Exchange delivery will see a gap.
3. Drain the input queue. Stop consuming new price ticks and wait for the in-flight queue depth to
   reach zero. This typically takes 10 to 30 seconds.
4. Wait for in-flight calculations to complete. The engine reports `calculations_pending: 0` when
   it is safe to proceed.
5. Perform a rolling restart, one region at a time. Wait for each region to report healthy before
   moving to the next.
6. Verify the first published value after restart against the last value before it. A discontinuity
   larger than the expected tick indicates the divisor cache did not reload correctly.
7. Confirm exchange delivery has resumed and the sequence numbers are contiguous.

## Never do this

Never restart all regions simultaneously during market hours. Every index the engine calculates
stops publishing for the duration, and exchanges will flag the gap.

## After the restart

Record the restart in the change log. The change specialist in an RCA investigation reads that log,
and an unrecorded restart makes a later anomaly much harder to explain.
