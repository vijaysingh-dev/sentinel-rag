"""Read-only warehouse access for the data specialist.

Generated SQL executed against a production warehouse is the highest-risk tool in the system, so
it is defended in four independent layers rather than by prompt instruction alone:

1. **Parse, do not regex.** `sqlglot` builds an AST and the statement type is checked on the tree.
   A regex looking for "DELETE" is defeated by comments, casing and string literals; a parser is not.
2. **Single statement only.** Stacked queries are the classic injection vector.
3. **Forced row limit.** An unbounded SELECT on a large fact table is a self-inflicted outage.
4. **Database-level read-only role.** The one that actually matters. The first three are defence in
   depth; this is the guarantee. Prompt instructions are not a security control.
"""

from __future__ import annotations

import hashlib
import re

from langchain_core.tools import tool

from sentinel.core.config import settings
from sentinel.core.observability import TOOL_CALLS, log
from sentinel.rca.runtime import current_tenant, fixture_lookup

FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|grant|revoke|copy|truncate|vacuum|call|do)\b",
    re.IGNORECASE,
)


class UnsafeQuery(ValueError):
    """Raised when generated SQL fails validation. Returned to the model, never to the database."""


def validate_sql(sql: str) -> str:
    """Rejects anything that is not a single bounded SELECT and forces a row limit."""
    import sqlglot

    stripped = sql.strip().rstrip(";")
    if FORBIDDEN.search(stripped):
        raise UnsafeQuery("write or DDL statement rejected")
    if ";" in stripped:
        raise UnsafeQuery("multiple statements rejected")

    parsed = sqlglot.parse(stripped, read="postgres")
    if len(parsed) != 1 or parsed[0] is None or parsed[0].key.lower() not in {"select", "with"}:
        raise UnsafeQuery("only a single SELECT is permitted")

    if not re.search(r"\blimit\b", stripped, re.IGNORECASE):
        stripped = f"{stripped} LIMIT {settings.sql_row_limit}"
    return stripped


def _ref(sql: str) -> str:
    """Content-derived citation handle, so a claim can be traced to the exact query."""
    return "sql:" + hashlib.sha256(sql.encode()).hexdigest()[:12]


@tool
async def run_readonly_sql(sql: str, purpose: str) -> dict:
    """Run a single read-only SELECT against the index metrics warehouse.

    Use this to compare an index against its own history or against peer indices, to inspect
    divisor and constituent changes, and to check whether an upstream feed arrived on time.

    Tables available: index_values, index_constituents, divisor_history, feed_arrivals,
    corporate_actions. All are partitioned by trade_date — always filter on it.

    Args:
        sql: A single SELECT statement. No writes, no DDL, no stacked statements.
        purpose: One line explaining what this query establishes. Recorded in the audit log.
    """
    recorded = fixture_lookup("run_readonly_sql")
    if recorded is not None:
        TOOL_CALLS.labels("run_readonly_sql", "fixture").inc()
        return recorded

    try:
        safe_sql = validate_sql(sql)
    except UnsafeQuery as exc:
        TOOL_CALLS.labels("run_readonly_sql", "rejected").inc()
        log.warning("sql_rejected", reason=str(exc), purpose=purpose)
        return {
            "error": "query rejected",
            "reason": str(exc),
            "instruction": "Rewrite as a single bounded SELECT and try once more.",
        }

    try:
        from sentinel.rca.db import warehouse_pool

        with warehouse_pool().connection() as conn, conn.cursor() as cur:
            # Tenant scoping is applied here from the authenticated context, not taken from the
            # model's SQL, so a generated query cannot reach another tenant's rows.
            cur.execute("SET LOCAL sentinel.tenant_id = %s", (current_tenant(),))
            cur.execute(safe_sql)
            columns = [d.name for d in cur.description]
            rows = [dict(zip(columns, r, strict=True)) for r in cur.fetchall()]
    except Exception as exc:
        TOOL_CALLS.labels("run_readonly_sql", "error").inc()
        log.warning("sql_failed", error=type(exc).__name__, purpose=purpose)
        return {
            "error": "warehouse unavailable",
            "instruction": "Continue with other tools and note that warehouse data could not be checked.",
        }

    TOOL_CALLS.labels("run_readonly_sql", "ok").inc()
    log.info("sql_executed", ref=_ref(safe_sql), rows=len(rows), purpose=purpose)
    return {"ref": _ref(safe_sql), "row_count": len(rows), "rows": rows[:100], "truncated": len(rows) > 100}
