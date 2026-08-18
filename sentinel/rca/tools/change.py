"""Deployment, configuration and corporate-action lookups for the change specialist.

These are stubbed against the platform's own services. What matters architecturally is the shape:
each returns a `ref` so claims are traceable, each fails soft with an instruction rather than an
exception, and each is scoped by the authenticated tenant rather than by a model-supplied argument.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from langchain_core.tools import tool

from sentinel.core.observability import TOOL_CALLS, log
from sentinel.rca.runtime import current_tenant, fixture_lookup


@tool
async def recent_deployments(service: str | None = None, hours: int = 24) -> dict:
    """List deployments and configuration changes in a recent window.

    Correlation in time is a lead, not a cause — report the timing and let the evidence stand.

    Args:
        service: Optional service filter, e.g. 'ice', 'data-feed', 'exchange-delivery'.
        hours: How far back to look. Default 24.
    """
    recorded = fixture_lookup("recent_deployments")
    if recorded is not None:
        TOOL_CALLS.labels("recent_deployments", "fixture").inc()
        return recorded

    try:
        from sentinel.rca.clients import deploy_client

        since = datetime.utcnow() - timedelta(hours=hours)
        events = await deploy_client().list_changes(tenant_id=current_tenant(), service=service, since=since)
    except Exception as exc:
        TOOL_CALLS.labels("recent_deployments", "error").inc()
        log.warning("deploy_lookup_failed", error=type(exc).__name__)
        return {
            "error": "deployment service unavailable",
            "instruction": "Note that recent changes could not be checked.",
        }

    TOOL_CALLS.labels("recent_deployments", "ok").inc()
    return {
        "ref": f"deploy:{service or 'all'}:{hours}h",
        "count": len(events),
        "events": events[:25],
    }


@tool
async def corporate_actions(index_id: str, days: int = 5) -> dict:
    """Corporate actions affecting an index's constituents in a recent window.

    Dividends, splits, spin-offs and their processing status. Unprocessed or mis-timed corporate
    actions are one of the most common causes of divisor and index-value anomalies.

    Args:
        index_id: The index ticker, e.g. 'EU_TECH_50'.
        days: Lookback window in days. Default 5.
    """
    recorded = fixture_lookup("corporate_actions")
    if recorded is not None:
        TOOL_CALLS.labels("corporate_actions", "fixture").inc()
        return recorded

    try:
        from sentinel.rca.clients import reference_data_client

        actions = await reference_data_client().corporate_actions(
            tenant_id=current_tenant(), index_id=index_id, days=days
        )
    except Exception as exc:
        TOOL_CALLS.labels("corporate_actions", "error").inc()
        return {
            "error": "reference data unavailable",
            "instruction": "Note that corporate actions could not be checked.",
        }

    TOOL_CALLS.labels("corporate_actions", "ok").inc()
    return {"ref": f"ca:{index_id}:{days}d", "count": len(actions), "actions": actions[:25]}


@tool
async def feed_status(feed_name: str | None = None) -> dict:
    """Current health of upstream data feeds: arrival times, lag and completeness.

    This reads live state, so its output is never cached — see `core/cache.LIVE_TOOLS`.

    Args:
        feed_name: Optional feed filter. Omit for a platform-wide view.
    """
    recorded = fixture_lookup("feed_status")
    if recorded is not None:
        TOOL_CALLS.labels("feed_status", "fixture").inc()
        return recorded

    try:
        from sentinel.rca.clients import monitor_client

        status = await monitor_client().feed_health(tenant_id=current_tenant(), feed=feed_name)
    except Exception as exc:
        TOOL_CALLS.labels("feed_status", "error").inc()
        return {"error": "monitoring unavailable", "instruction": "Note that live feed health could not be checked."}

    TOOL_CALLS.labels("feed_status", "ok").inc()
    return {"ref": f"feed:{feed_name or 'all'}", "status": status}
