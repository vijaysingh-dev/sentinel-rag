# Parsing and embedding cost review — restricted

**Classification:** Internal — restricted. Admin access only.

## Why this is restricted

It contains per-provider unit pricing under our negotiated contract terms, which we are not
permitted to share outside the account team.

## Summary

On the current corpus mix, hybrid_escalate routes about a fifth of documents to a cloud parser.
The scanned escalation card and the two-column topology PDF account for most of the escalations;
the runbooks and ADRs never escalate. Moving the topology document to a single-column template
would drop the escalation rate materially at no quality cost.

## Recommendation

Keep hybrid_escalate. Do not switch to hybrid_merge for the whole corpus — paying the cloud cost
on every runbook to improve two documents is the wrong trade. Fix the two documents instead.
