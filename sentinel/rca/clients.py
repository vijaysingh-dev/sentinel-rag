"""Clients for the platform services the RCA agent reads from.

Stubbed here — the real implementations call GRIP's own APIs. What matters architecturally, and
what the stubs preserve, is the shape:

* every method is async and returns plain data, not exceptions, for the *expected* empty cases;
* infrastructure failures raise so `guarded()` can trip a breaker and degrade;
* nothing here writes. The only write in the system is `itsm_client().attach_rca`, and it sits
  behind the human interrupt.
"""

from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from typing import Any

from sentinel.core.resilience import guarded


class DeployClient:
    """Deployment and configuration change history."""

    @guarded("deploy_service", timeout_s=15.0)
    async def list_changes(self, tenant_id: str, service: str | None, since: datetime) -> list[dict]:
        raise NotImplementedError("wire to the platform deployment API")


class ReferenceDataClient:
    """Corporate actions and constituent reference data."""

    @guarded("reference_data", timeout_s=15.0)
    async def corporate_actions(self, tenant_id: str, index_id: str, days: int) -> list[dict]:
        raise NotImplementedError("wire to the reference data service")


class MonitorClient:
    """Live platform health: feeds, ICE, exchange delivery, Kafka lag.

    Everything here is live state, so `core/cache.LIVE_TOOLS` excludes it from every cache layer.
    A cached health check is worse than no health check.
    """

    @guarded("system_monitor", timeout_s=10.0)
    async def feed_health(self, tenant_id: str, feed: str | None) -> dict:
        raise NotImplementedError("wire to the system monitor API")


class ITSMClient:
    """The single write path, reached only after human approval."""

    @guarded("itsm", timeout_s=20.0)
    async def attach_rca(self, anomaly_id: str, tenant_id: str, draft: dict, approved_by: str) -> str:
        raise NotImplementedError("wire to the incident management system")


@lru_cache
def deploy_client() -> DeployClient:
    return DeployClient()


@lru_cache
def reference_data_client() -> ReferenceDataClient:
    return ReferenceDataClient()


@lru_cache
def monitor_client() -> MonitorClient:
    return MonitorClient()


@lru_cache
def itsm_client() -> ITSMClient:
    return ITSMClient()
