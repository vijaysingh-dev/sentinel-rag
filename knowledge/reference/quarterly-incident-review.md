# Quarterly incident review

17 incidents recorded, 2026-06-08 through 2026-09-07. Generated from `scripts/corpus/_incident_truth.jsonl` — every incident this document aggregates has its own full post-mortem in `knowledge/incident/`; this page is the roll-up, not a replacement for reading the individual write-ups.

## By incident type

| Type | Count | What it means |
| --- | --- | --- |
| single | 13 | one fault flag, one root cause |
| compound | 2 | two faults active in the same window, independently |
| organic | 2 | no fault flag — a deploy or a resource limit |

## By responding team

| Team | Incidents caused |
| --- | --- |
| checkout | 8 |
| catalog | 7 |
| data | 2 |
| storefront | 1 |
| platform-eng | 1 |

## By service

| Service | Incidents caused |
| --- | --- |
| `ad` | 3 |
| `payment` | 2 |
| `kafka` | 2 |
| `product-catalog` | 2 |
| `recommendation` | 2 |
| `cart` | 2 |
| `email` | 2 |
| `frontend` | 1 |
| `shipping` | 1 |
| `currency` | 1 |
| `astronomy-db` | 1 |

## Repeat offenders

A service showing up more than once in one review period is a signal no single post-mortem's action items can surface on its own — check its runbook's Known failure modes section for whether the recurring cause is the same flag each time or a different one.

- `ad` — 3 incidents (INC-2614, INC-2635, INC-2649)
- `payment` — 2 incidents (INC-2607, INC-2642)
- `kafka` — 2 incidents (INC-2621, INC-2707)
- `product-catalog` — 2 incidents (INC-2628, INC-2714)
- `recommendation` — 2 incidents (INC-2656, INC-2714)
- `cart` — 2 incidents (INC-2663, INC-2684)
- `email` — 2 incidents (INC-2677, INC-2707)

## Notable

- 2 compound incident(s) this period — two independent faults overlapping in time. These take longer to close than a single-fault incident of the same severity because the investigation has to first establish that it is looking at two things, not one; see each compound post-mortem's "Contributing factors" section for the specific reasoning that split them.
- 2 incident(s) this period had no fault flag at all — a deploy or a resource limit, not an injection. flagd coming back empty is not by itself grounds to stop investigating; both organic post-mortems record time lost to exactly that assumption.
