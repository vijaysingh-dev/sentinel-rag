"""Build the entire knowledge corpus and its ground truth.

    uv run python scripts/corpus/build_all.py [--clean]

Runs every generator in dependency order (incidents before runbooks, because the runbooks embed
an incident cross-reference), then prints a summary of what the corpus now contains by doc_type
and format. `--clean` removes generated files first so a rename in a generator does not leave a
stale document behind.

This is what `make corpus` calls.
"""

from __future__ import annotations

import argparse
import importlib
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from _model import KNOWLEDGE, REPO_ROOT  # noqa: E402

# order matters: incidents writes _incident_index.json and _incident_truth.jsonl; runbooks,
# gen_playbook's deploy history, and gen_incident_review all read one or the other of those.
GENERATORS = [
    "gen_incidents",
    "gen_runbooks",
    "gen_architecture",
    "gen_platform",
    "gen_adr",
    "gen_glossary",
    "gen_reference",
    "gen_playbook",
    "gen_incident_review",
    "gen_messy",
    "gen_restricted",
    "gen_ground_truth",
    "gen_golden",
    "gen_agent_cases",
]

# doc_type dirs the generators own. --clean only ever touches these.
GENERATED_DIRS = [
    "runbook", "incident", "architecture", "platform", "adr", "glossary", "oncall", "reference", "restricted",
]


def _clean() -> None:
    removed = 0
    for d in GENERATED_DIRS:
        for f in (KNOWLEDGE / d).glob("*"):
            if f.is_file():
                f.unlink()
                removed += 1
    for f in (REPO_ROOT / "data" / "ground_truth").glob("*.json"):
        f.unlink()
        removed += 1
    print(f"clean: removed {removed} generated files\n")


def _summary() -> None:
    by_type: Counter[str] = Counter()
    by_ext: Counter[str] = Counter()
    for f in KNOWLEDGE.rglob("*"):
        if f.is_file() and not f.name.startswith("_"):
            by_type[f.parent.name] += 1
            by_ext[f.suffix.lstrip(".") or "none"] += 1

    print("\n" + "=" * 60)
    print("corpus summary")
    print("=" * 60)
    print("\nby doc_type:")
    for t, n in sorted(by_type.items()):
        print(f"  {t:16s} {n}")
    print("\nby format:")
    for e, n in sorted(by_ext.items(), key=lambda kv: -kv[1]):
        print(f"  {e:16s} {n}")
    total = sum(by_type.values())
    truth = len(list((REPO_ROOT / "data" / "ground_truth").glob("*.json")))
    print(f"\n  {total} documents, {truth} ground-truth files")
    print("\nnext: uv run python -m sentinel.ingest.cli knowledge --recreate")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--clean", action="store_true", help="remove generated files first")
    args = ap.parse_args()

    if args.clean:
        _clean()

    # generators call argparse in main(); run them with a clean argv so build_all's own flags
    # (and --clean) do not leak in.
    saved_argv = sys.argv
    sys.argv = [sys.argv[0]]
    for name in GENERATORS:
        print(f"--- {name} " + "-" * (54 - len(name)))
        mod = importlib.import_module(name)
        rc = mod.main()
        if rc != 0:
            sys.argv = saved_argv
            print(f"\n{name} failed with exit code {rc}", file=sys.stderr)
            return rc
        print()
    sys.argv = saved_argv

    _summary()
    return 0


if __name__ == "__main__":
    sys.exit(main())
