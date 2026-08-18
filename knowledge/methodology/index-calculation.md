# Index Calculation

## Calculation cycle

Real-time indices are recalculated on every constituent price tick, subject to a minimum
publication interval. The cycle is:

1. Receive a price tick from the consolidated feed.
2. Validate against the previous price. Ticks exceeding the configured deviation band are held for
   review rather than applied, because a fat-finger price applied to an index is visible to every
   downstream consumer within milliseconds.
3. Update the constituent's contribution to aggregate market value.
4. Recompute the index level as aggregate market value divided by the divisor.
5. Apply the publication interval. Values are published at most once per interval regardless of
   tick rate.
6. Deliver to exchanges and the data feed.

## Publication intervals

| Index family | Interval |
| --- | --- |
| Headline equity | 1 second |
| Sector | 5 seconds |
| Strategy | 15 seconds |
| End-of-day only | on close |

## Calculation windows

An index calculates only during its defined window, which follows the primary listing venue's
trading hours plus any configured pre-open and post-close extensions. Outside the window the last
published value is held.

Indices with constituents across multiple time zones use a composite window covering the union of
venue hours. During periods when only some venues are open, closed-venue constituents contribute
their last traded price.

## Validation

Every published value passes three checks:

- **Continuity.** The change from the previous value is within the expected band for the elapsed
  time. Failures raise a `zscore_5m` detector alert.
- **Constituent completeness.** At least 95% of constituents have current prices.
- **Divisor freshness.** The divisor in use matches the divisor effective for the current date.

A value failing any check is held and the index is flagged rather than published. Publishing a
value known to be suspect is worse than publishing nothing, because downstream consumers cannot
distinguish it from a good one.
