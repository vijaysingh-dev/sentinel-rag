# Deploy history

Every deploy in Astroshop's history that a post-mortem names as a root cause. Not a full release log — this corpus has no use for a deploy that didn't cause an incident, so only the two below exist. Each row's note and timestamp come from `scripts/corpus/_incident_truth.jsonl` (gen_incidents.py) so this document and the post-mortem that cites it describe the same event.

| Date | Service | Team | Note | Incident |
| --- | --- | --- | --- | --- |
| 2026-09-03 | `currency` | checkout | The 14:20 deploy added a synchronous call to an external exchange-rate refresh on every conversion request, instead of reading the rate cached at startup. | INC-2767 |
| 2026-09-07 | `astronomy-db` | platform-eng | product-catalog's connection pool to astronomy-db was sized for steady-state traffic; a routine traffic ramp from the load generator exceeded it, and requests queued for a free connection instead of failing fast. | INC-2774 |
