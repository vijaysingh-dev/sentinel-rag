"""Chat message -> RCA anomaly dict.

`sentinel.rca.state.new_state`/`intake_node` require `anomaly` to carry `alerting_service`,
`started_at` and `signal` (`sentinel/rca/graph.py`'s `REQUIRED_ANOMALY_FIELDS`) — the RCA graph is
anomaly-shaped, not chat-shaped, because it was built to investigate a real alert. This module is
the seam: by the time it runs, `router.classify()` has already resolved which service the message
is about (or short-circuited to a clarifying question before ever reaching here — see
`router.py`'s `RouteDecision.clarifying_question`), so this is pure shaping, not inference.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sentinel.chat.router import RouteDecision


def build_from_decision(message: str, decision: RouteDecision) -> dict:
    """Synthesizes an anomaly dict from an already-resolved routing decision.

    Never called when `decision.inferred_service` is unset — `api.py`'s rca branch checks
    `clarifying_question` first and returns without reaching this function.
    """
    assert decision.inferred_service, "build_from_decision requires an inferred service"
    return {
        "summary": message,
        "alerting_service": decision.inferred_service,
        "started_at": datetime.now(UTC).isoformat(),
        "signal": decision.inferred_signal or "error_ratio",
    }
