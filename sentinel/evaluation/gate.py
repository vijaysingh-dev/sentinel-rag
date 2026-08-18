"""CI gates.

Turns evaluation numbers into a pass/fail decision, which is what makes evaluation a *process*
rather than a report nobody reads.

Two tiers, because they answer different questions:

* **Absolute thresholds** — "is this good enough to ship at all?" Cheap to reason about, but they
  drift: teams lower them under deadline pressure.
* **Regression thresholds** — "is this worse than what is already in production?" This is the one
  that actually catches problems, because it compares against a baseline committed to the
  repository rather than against a number someone chose once.

Safety checks are separate and are never averaged. A 98% pass rate on "did not leak restricted
content" is a failing grade.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from sentinel.core.observability import log

# Absolute floors for the pull-request gate. Tuned to be achievable but not trivial; raise them as
# the system improves, and record the change in the commit that raises them.
THRESHOLDS: dict[str, float] = {
    "recall@5": 0.75,
    "mrr": 0.65,
    "faithfulness": 0.90,
    "citation_validity": 0.85,
    "refusal_accuracy": 0.90,
    "route_accuracy": 0.80,
}

# A metric may fall this far below the committed baseline before it counts as a regression. Small
# enough to catch real degradation, large enough to absorb LLM-judge noise on a modest golden set.
REGRESSION_TOLERANCE = 0.03


@dataclass
class GateResult:
    passed: bool
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)
    baseline: dict[str, float] = field(default_factory=dict)

    def report(self) -> str:
        lines = ["PASS" if self.passed else "FAIL"]
        for key, value in sorted(self.metrics.items()):
            base = self.baseline.get(key)
            delta = f" ({value - base:+.3f} vs baseline)" if base is not None else ""
            lines.append(f"  {key}: {value:.3f}{delta}")
        for failure in self.failures:
            lines.append(f"  FAIL {failure}")
        for warning in self.warnings:
            lines.append(f"  WARN {warning}")
        return "\n".join(lines)


def check_gate(
    metrics: dict[str, float],
    safety_violations: list[dict] | None = None,
    baseline_path: str | Path = "benchmarks/baseline.json",
    thresholds: dict[str, float] | None = None,
) -> GateResult:
    thresholds = thresholds or THRESHOLDS
    failures: list[str] = []
    warnings: list[str] = []

    for violation in safety_violations or []:
        failures.append(f"safety: {violation.get('kind')} — {violation.get('detail', '')}")

    for key, floor in thresholds.items():
        value = metrics.get(key)
        if value is None:
            warnings.append(f"{key} not measured")
            continue
        if value < floor:
            failures.append(f"{key}={value:.3f} below floor {floor}")

    baseline: dict[str, float] = {}
    path = Path(baseline_path)
    if path.exists():
        baseline = json.loads(path.read_text())
        for key, base in baseline.items():
            value = metrics.get(key)
            if value is None:
                continue
            if value < base - REGRESSION_TOLERANCE:
                failures.append(f"{key} regressed: {value:.3f} vs baseline {base:.3f}")
    else:
        warnings.append(f"no baseline at {path}; regression checks skipped")

    result = GateResult(passed=not failures, failures=failures, warnings=warnings, metrics=metrics, baseline=baseline)
    log.info("gate_evaluated", passed=result.passed, failures=len(failures), warnings=len(warnings))
    return result


def write_baseline(metrics: dict[str, float], path: str | Path = "benchmarks/baseline.json") -> None:
    """Commits the current metrics as the new baseline.

    Intended to be run deliberately and committed to git, so that raising the bar is a reviewable
    change rather than something that happens quietly on someone's laptop.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(metrics, indent=2, sort_keys=True) + "\n")
    log.info("baseline_written", path=str(target), metrics=len(metrics))
