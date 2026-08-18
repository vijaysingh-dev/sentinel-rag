"""Shared benchmark harness: result recording, CSV output and leaderboard rendering.

Written once so every stage bake-off produces the same schema. That uniformity is what makes the
results comparable over time and what lets `docs/STAGE-OVERVIEW.md` be regenerated rather than
hand-maintained.

One convention worth keeping: **always record cost and latency alongside quality.** A provider that
wins on quality by 0.01 for eight times the price has not won, and a leaderboard that shows only
quality will let you talk yourself into it.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Sequence
from pathlib import Path

from sentinel.core.types import StageResult

RESULTS_DIR = Path("benchmarks/results")


def record(results: Sequence[StageResult], name: str) -> Path:
    """Appends results to a per-stage CSV and returns its path."""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULTS_DIR / f"{name}.csv"

    rows = []
    for result in results:
        row = {
            "timestamp": result.timestamp,
            "stage": result.stage,
            "provider": result.provider,
            "config": json.dumps(result.config, sort_keys=True),
            "duration_s": round(result.duration_s, 3),
            "usd_cost": round(result.usd_cost, 6),
            "n_items": result.n_items,
            "notes": result.notes,
        }
        row.update({f"m_{k}": round(v, 4) for k, v in result.metrics.items()})
        rows.append(row)

    if not rows:
        return path

    fieldnames = sorted({k for row in rows for k in row})
    exists = path.exists()

    # Header drift across runs is handled by rewriting when the columns change, so an added metric
    # does not corrupt the file.
    if exists:
        with path.open(newline="", encoding="utf-8") as fh:
            existing_fields = next(csv.reader(fh), [])
        if set(existing_fields) != set(fieldnames):
            old = list(csv.DictReader(path.open(newline="", encoding="utf-8")))
            fieldnames = sorted({k for r in [*old, *rows] for k in r})
            rows = [*old, *rows]
            exists = False

    with path.open("a" if exists else "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        writer.writerows(rows)

    return path


def leaderboard(
    results: Sequence[StageResult],
    sort_by: str,
    columns: Sequence[str] | None = None,
    higher_is_better: bool = True,
) -> str:
    """Renders a markdown table, best first.

    The `sort_by` metric decides the ranking, but every metric is shown — because the second-best
    configuration is often the right choice once you look at the cost column.
    """
    if not results:
        return "_no results_"

    metric_names = columns or sorted({k for r in results for k in r.metrics})
    ordered = sorted(results, key=lambda r: r.metrics.get(sort_by, 0), reverse=higher_is_better)

    header = ["#", "provider", "config", *metric_names, "secs", "usd"]
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]

    for rank, result in enumerate(ordered, start=1):
        config = ", ".join(f"{k}={v}" for k, v in sorted(result.config.items()))
        cells = [
            str(rank),
            result.provider,
            config or "-",
            *[f"{result.metrics.get(m, 0):.4f}" for m in metric_names],
            f"{result.duration_s:.2f}",
            f"{result.usd_cost:.4f}",
        ]
        lines.append("| " + " | ".join(cells) + " |")

    return "\n".join(lines)


def value_analysis(results: Sequence[StageResult], quality_metric: str, baseline_provider: str = "local") -> str:
    """Reports cost per unit of quality gained over a free baseline.

    This is the analysis that turns a benchmark into a decision. A paid provider is only worth it
    if the quality gain is real and the price per point is defensible at your corpus size.
    """
    baseline = next((r for r in results if r.provider == baseline_provider), None)
    if baseline is None:
        return "_no baseline provider in results_"

    base_score = baseline.metrics.get(quality_metric, 0)
    lines = [f"Baseline: {baseline_provider} at {quality_metric}={base_score:.4f}, ${baseline.usd_cost:.4f}", ""]

    for result in results:
        if result.provider == baseline_provider:
            continue
        gain = result.metrics.get(quality_metric, 0) - base_score
        extra_cost = result.usd_cost - baseline.usd_cost
        if gain <= 0:
            lines.append(f"- **{result.provider}**: no gain ({gain:+.4f}) — not worth paying for")
        elif extra_cost <= 0:
            lines.append(f"- **{result.provider}**: +{gain:.4f} at no extra cost — take it")
        else:
            lines.append(
                f"- **{result.provider}**: +{gain:.4f} for ${extra_cost:.4f} (${extra_cost / gain:.4f} per point)"
            )

    return "\n".join(lines)


def write_markdown(path: str | Path, sections: dict[str, str]) -> Path:
    """Writes a benchmark report."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    body = "\n\n".join(f"## {title}\n\n{content}" for title, content in sections.items())
    target.write_text(f"# Benchmark results\n\n{body}\n", encoding="utf-8")
    return target
