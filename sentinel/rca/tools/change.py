"""Change-history lookups for the change specialist: flags, deploys, and the incident log.

These are stubbed against the otel-demo's control plane (flagd, the CD pipeline, the
incident-generator). What matters architecturally is the shape: each returns a `ref` so claims
are traceable, each fails soft with an instruction rather than an exception, and each is scoped by
the authenticated tenant rather than by a model-supplied argument.

`current_flags` reads live state, so its output is never cached — see `core/cache.LIVE_TOOLS`.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from langchain_core.tools import tool

from sentinel.core.observability import TOOL_CALLS, log
from sentinel.rca.runtime import current_tenant, fixture_lookup


@tool
async def current_flags(only_changed: bool = True) -> dict:
    """The current feature-flag configuration, and which flags are off their default.

    On this platform every injectable fault is a flag flip, so this is usually the fastest route
    to a cause: if a flag is set and its change time lines up with the alert, that is the answer.
    Live state — never cached.

    Args:
        only_changed: Return only flags that are not on their default variant. Default true.
    """
    recorded = fixture_lookup("current_flags")
    if recorded is not None:
        TOOL_CALLS.labels("current_flags", "fixture").inc()
        return recorded

    try:
        from sentinel.rca.clients import flag_client

        flags = await flag_client().current_flags(only_non_default=only_changed)
    except Exception as exc:
        TOOL_CALLS.labels("current_flags", "error").inc()
        log.warning("flag_lookup_failed", error=type(exc).__name__)
        return {
            "error": "flag service unavailable",
            "instruction": "Note that the current flag configuration could not be checked.",
        }

    TOOL_CALLS.labels("current_flags", "ok").inc()
    return {"ref": "flags:current", "flags": flags}


@tool
async def flag_changes(hours: int = 6) -> dict:
    """Feature-flag changes in a recent window, with timestamps.

    A flag whose change time is a few minutes before the anomaly is a strong lead. Correlation in
    time is still a lead, not a proof — report the timing and let it stand.

    Args:
        hours: How far back to look. Default 6.
    """
    recorded = fixture_lookup("flag_changes")
    if recorded is not None:
        TOOL_CALLS.labels("flag_changes", "fixture").inc()
        return recorded

    try:
        from sentinel.rca.clients import flag_client

        since = datetime.utcnow() - timedelta(hours=hours)
        changes = await flag_client().flag_changes(since=since)
    except Exception:
        TOOL_CALLS.labels("flag_changes", "error").inc()
        return {
            "error": "flag audit log unavailable",
            "instruction": "Note that recent flag changes could not be checked.",
        }

    TOOL_CALLS.labels("flag_changes", "ok").inc()
    return {"ref": f"flagchg:{hours}h", "count": len(changes), "changes": changes[:25]}


@tool
async def recent_deployments(service: str | None = None, hours: int = 24) -> dict:
    """Deployments for a service in a recent window — image-tag changes and rollouts.

    A deploy shortly before the anomaly is a lead. If one correlates, the mitigation is usually a
    rollback; say "deployed 11 minutes before the alert" and let the evidence stand.

    Args:
        service: Optional service filter, e.g. 'checkout', 'product-catalog'.
        hours: How far back to look. Default 24.
    """
    recorded = fixture_lookup("recent_deployments")
    if recorded is not None:
        TOOL_CALLS.labels("recent_deployments", "fixture").inc()
        return recorded

    try:
        from sentinel.rca.clients import deploy_client

        since = datetime.utcnow() - timedelta(hours=hours)
        events = await deploy_client().list_deploys(service=service, since=since)
    except Exception as exc:
        TOOL_CALLS.labels("recent_deployments", "error").inc()
        log.warning("deploy_lookup_failed", error=type(exc).__name__)
        return {
            "error": "deployment service unavailable",
            "instruction": "Note that recent deployments could not be checked.",
        }

    TOOL_CALLS.labels("recent_deployments", "ok").inc()
    return {
        "ref": f"deploy:{service or 'all'}:{hours}h",
        "count": len(events),
        "events": events[:25],
    }


@tool
async def incident_history(limit: int = 10) -> dict:
    """Recent incidents from the platform's incident log.

    On this platform the log records the fault scenario that was run and when — the scheduled
    fault-injection events. Use it to check whether the current symptom matches a scenario that
    is running or recently ran.

    Args:
        limit: How many recent incidents to return. Default 10.
    """
    recorded = fixture_lookup("incident_history")
    if recorded is not None:
        TOOL_CALLS.labels("incident_history", "fixture").inc()
        return recorded

    try:
        from sentinel.rca.clients import incident_log_client

        incidents = await incident_log_client().recent_incidents(limit=min(limit, 50))
    except Exception:
        TOOL_CALLS.labels("incident_history", "error").inc()
        return {
            "error": "incident log unavailable",
            "instruction": "Note that the incident log could not be checked.",
        }

    TOOL_CALLS.labels("incident_history", "ok").inc()
    return {
        "ref": f"inclog:{limit}",
        "count": len(incidents),
        "incidents": incidents[:limit],
        "tenant": current_tenant(),
    }
