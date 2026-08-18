# Divisor Adjustment Methodology

The divisor is the scaling factor that keeps an index level continuous across events that change
the aggregate market value of its constituents without reflecting a change in the market itself.

## Principle

An index level is the aggregate market value of constituents divided by the divisor. When a
corporate action changes aggregate market value for a non-market reason, the divisor is adjusted so
the index level immediately before and immediately after the event is identical. Only genuine price
movement should move the index.

## Events requiring an adjustment

**Special dividends.** A special dividend is a distribution outside the normal dividend policy. The
divisor is adjusted on the ex-date, because the share price drops by approximately the distribution
amount for reasons unrelated to market sentiment.

**Spin-offs.** When a constituent distributes shares of a subsidiary, the parent's price falls by
the value of the distributed entity. The divisor is adjusted on the ex-date. If the spun-off entity
qualifies for index membership it is added simultaneously.

**Rights issues.** Adjusted on the ex-rights date using the theoretical ex-rights price.

**Share count changes.** Buybacks and secondary offerings change shares outstanding and therefore
aggregate market value. Adjusted on the effective date.

**Constituent changes.** Additions, deletions and weight changes at rebalance are all divisor
events.

## Events not requiring an adjustment

**Ordinary dividends.** In a price-return index, ordinary dividends are not adjusted for. The index
level declines naturally on the ex-date, and that decline is the intended behaviour — a price-return
index measures price, not total return.

This is the distinction most often misunderstood. A total-return variant of the same index *does*
account for ordinary dividends, by reinvesting them rather than by adjusting the divisor.

**Stock splits.** A split changes the share price and the share count proportionally, so aggregate
market value is unchanged and no divisor adjustment is needed. The constituent's price and shares
are both updated.

## Failure modes

An unprocessed or mis-timed divisor adjustment produces an index level that is wrong by roughly the
proportional size of the event, and it persists until corrected. It typically presents as a step
change at the market open on the ex-date, affecting one index while its peers are unaffected — which
is the signature that distinguishes it from a market move or a feed problem.
