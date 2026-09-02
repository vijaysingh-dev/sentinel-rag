"""Generate the architecture decision records.

    uv run python scripts/corpus/gen_adr.py

Unlike the runbooks and post-mortems, these are not derived from services.yaml — they are the
handful of real decisions behind how this platform and its RCA system are built, written out in
the standard ADR form (context / decision / alternatives / consequences). They exist in the
corpus because "why is it done this way?" is a question the role-based chatbot should be able to
answer from a citable source, and because the ADR doc_type exercises the structural chunker's
heading-path handling.

Each ADR mirrors a real invariant or design choice already present in the repo — the CLAUDE.md
non-negotiables, the health-rollup exclusions, the observability boundary — so the corpus and the
code agree.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

from _model import load, rel, write

sys.path.insert(0, str(Path(__file__).parent))

ADRS: list[dict] = [
    {
        "num": 1,
        "slug": "flagd-for-fault-injection",
        "title": "Inject faults by flag file, not by an API or a chaos tool",
        "status": "Accepted",
        "context": """
            The platform needs a way to produce realistic failures on demand, both for the
            incident-generator's scheduled scenarios and for a human running one by hand. The
            options were a dedicated chaos-engineering tool (Chaos Mesh, Litmus), a fault-injection
            API added to each service, or reusing the feature-flag daemon (flagd) that is already
            in the deployment for demo toggles.
            """,
        "decision": """
            Faults are flag flips. Each fault is a variant on a flag in demo.flagd.json; flagd
            watches that file and reloads on change, so setting a fault is a single-file edit with
            no restart and no API call. The incident-generator writes the same file on a schedule;
            flagd-ui edits it from a browser; a human can edit it directly. One mechanism, three
            front ends.
            """,
        "alternatives": [
            ("A chaos tool", "More realistic failure modes (network partitions, pod kills) but a "
                              "second control plane to run and secure, and its actions are not "
                              "trivially correlated with a ground-truth log."),
            ("A fault API per service", "Precise, but N services to modify and keep in sync, and "
                                        "the fault code ships in the production image."),
        ],
        "consequences": """
            Positive: the incident-generator's incidents.jsonl is exact ground truth — it records
            the flag it set and when, so the RCA agent can be scored against a known cause.
            Recovery is instant (next flag evaluation) so scenarios are cheap to run repeatedly.
            Negative: the fault surface is limited to what a flag can express — application-level
            errors, latency and resource pressure, but not infrastructure failures like a network
            partition or a disk filling. Those have to be tested another way. Also: nothing pages
            on a flag change, so every runbook carries a manual "check flagd" step and most
            post-mortems list the missing flag-change alert as a contributing factor.
            """,
    },
    {
        "num": 2,
        "slug": "exclude-collector-and-flagd-from-health-rollup",
        "title": "Exclude otel-collector and flagd from the dependency graph and health rollup",
        "status": "Accepted",
        "context": """
            app/system-monitor computes a health status for each service and propagates it up the
            dependency graph, so a single red leaf shows as degraded on every service that depends
            on it. The graph is derived from the deployment's own depends_on declarations. Almost
            every service declares a dependency on otel-collector (for telemetry) and flagd (for
            config).
            """,
        "decision": """
            otel-collector, flagd and the load-generator are excluded from the graph the rollup
            walks. The graph contains only business dependencies — the calls a service makes to do
            its job.
            """,
        "alternatives": [
            ("Include them", "Truthful to the compose file, but the rollup would then say "
                             "\"every service's parent is otel-collector\": one collector blip "
                             "turns the whole board red and hides which business service actually "
                             "failed."),
            ("Include the collector but weight it lower", "Adds a tuning knob and a second concept "
                                                          "(dependency strength) for one special case."),
        ],
        "consequences": """
            Positive: the health board reflects business impact — a red checkout means orders are
            failing, not that a sidecar restarted. The same reasoning is applied consistently in
            the RCA agent's scope boundary and in the architecture deck's dependency slide.
            Negative: a genuine collector outage (telemetry stops) is invisible to the rollup and
            has to be caught by a separate direct check on the collector itself. This is an
            accepted blind spot, documented here so it is a decision and not a surprise.
            """,
    },
    {
        "num": 3,
        "slug": "rca-reads-metrics-and-traces-not-logs",
        "title": "The RCA agent reads metrics and traces, not logs",
        "status": "Accepted",
        "context": """
            The RCA agent needs live signal about a running incident. The three telemetry
            signals available are metrics (Prometheus), traces (Jaeger) and logs. An agent with a
            log-search tool is straightforward to build.
            """,
        "decision": """
            The agent's live-state tools read Prometheus (which service's RED signals moved, and
            when) and Jaeger (which span in a failing request carries the error or the latency).
            There is no log-search tool.
            """,
        "alternatives": [
            ("Add a log-search tool", "High recall, low precision. An agent tends to anchor on the "
                                      "first plausible-looking log line and build a narrative around "
                                      "it. Log volume also makes context-window management hard."),
            ("Logs as a fallback only", "Still ships the failure mode above; the agent will reach "
                                        "for the fallback whenever metrics and traces are "
                                        "ambiguous, which is exactly when anchoring is most costly."),
        ],
        "consequences": """
            Positive: metrics and traces are low-cardinality, structured signals an agent can
            reason over without being flooded. The investigation stays on "which service, which
            span, what changed". Negative: some root causes are only visible in a log line (a
            specific exception message, a config value printed at startup). For those the agent
            produces a well-scoped hypothesis and hands off to a human with the trace id, rather
            than confirming it itself. This is a deliberate ceiling on the agent's autonomy.
            """,
    },
    {
        "num": 4,
        "slug": "corpus-excludes-live-state",
        "title": "Live state is never embedded into the knowledge index",
        "status": "Accepted",
        "context": """
            It is tempting to index everything: current metrics, recent deploys, the current flag
            configuration, open incidents. It would make the retriever a single entry point for
            every question.
            """,
        "decision": """
            The index contains only stable knowledge — runbooks, post-mortems, ADRs, the glossary,
            the architecture deck. Anything that changes minute to minute is fetched by an agent
            tool at investigation time and is never embedded. Live-state tools are additionally
            excluded from every cache layer (core/cache.LIVE_TOOLS).
            """,
        "alternatives": [
            ("Index live state with a short TTL and frequent re-ingestion", "A vector built from a "
             "metric value is stale the moment it is embedded; a short TTL just means constant "
             "re-indexing and a race between the index and reality."),
            ("Index deploy history only (it changes slowly)", "Reasonable for deploys, but it "
             "blurs the boundary — the next person adds 'and current flag state, it also changes "
             "slowly' and the principle is gone."),
        ],
        "consequences": """
            Positive: a retrieved chunk is always something that was true when written and is
            still meaningful now. The boundary is bright: if it has a timestamp that matters, it
            is a tool call, not a document. Negative: answering "why is checkout slow right now"
            requires the agent to combine a retrieved runbook with a live trace — neither alone is
            enough — so the agent cannot be a pure RAG system. That combination is the whole point
            of the agent layer.
            """,
    },
    {
        "num": 5,
        "slug": "acl-filters-inside-the-query",
        "title": "Access-control filters are applied inside the vector query, not after it",
        "status": "Accepted",
        "context": """
            The corpus has role-scoped content: some post-mortems name customers and monetary
            impact and are admin-only; some runbooks are scoped to the team that owns the service.
            The retriever must not return a chunk the caller's role may not see.
            """,
        "decision": """
            The role filter is a clause in the vector search query itself. The store never loads a
            restricted chunk into the process. An empty role list raises rather than matching
            everything (SearchFilter.require_scope), and a refusal never states that a restricted
            document exists.
            """,
        "alternatives": [
            ("Retrieve then filter in application code", "The restricted content has already been "
             "read into the process and logged in traces before the filter runs. One logging "
             "misconfiguration and it leaks."),
            ("A separate index per role", "No cross-role retrieval is possible even when it should "
             "be, and every document has to be routed to the right index at ingest — a routing "
             "bug is a silent leak."),
        ],
        "consequences": """
            Positive: restricted content is never in the process for an unauthorised caller, so a
            logging or serialisation bug cannot leak it. The scope comes from the request context,
            never a tool argument, so a prompt injection cannot widen it. Negative: the store
            adapter must support metadata filtering in the query (pgvector, Azure AI Search and
            OpenSearch all do); a store that does not could not be used without violating this.
            That constraint is accepted and noted in docs/adapters.md.
            """,
    },
    {
        "num": 6,
        "slug": "two-field-chunks",
        "title": "A chunk carries a separate embed_text and context_text",
        "status": "Accepted",
        "context": """
            The retriever wants small, single-topic units so the embedding vector is meaningful.
            The generator wants enough surrounding context to actually answer — the heading the
            step sits under, the sentence before the one that matched, the table's column headers.
            One text field cannot serve both.
            """,
        "decision": """
            Chunk has two text fields. embed_text is vectorised and kept tight. context_text is
            handed to the LLM and may be larger — the parent section, a sentence window, the whole
            procedure. They are equal by default and deliberately need not be.
            """,
        "alternatives": [
            ("One text field, tuned chunk size", "Every size is a compromise between the two "
             "consumers; sentence-window and parent-document strategies cannot be expressed at all."),
            ("Store only embed_text, re-fetch context at query time", "Doubles the read path and "
             "couples retrieval latency to a second lookup; the context is already known at chunk "
             "time, so store it then."),
        ],
        "consequences": """
            Positive: the retriever and the generator each get the text shaped for them; advanced
            chunking strategies become expressible. Negative: it is one more thing to get right —
            embed_chunks must embed embed_text and never context_text, and getting that backwards
            silently destroys retrieval quality with no error. That specific mistake is called out
            in CLAUDE.md and guarded in the ingest tests.
            """,
    },
]


def _render(adr: dict) -> str:
    def para(text: str) -> str:
        return textwrap.fill(" ".join(text.split()), width=98)

    L: list[str] = []
    L.append(f"# ADR-{adr['num']:03d}: {adr['title']}")
    L.append("")
    L.append(f"**Status:** {adr['status']}")
    L.append("")
    L.append("## Context")
    L.append("")
    L.append(para(adr["context"]))
    L.append("")
    L.append("## Decision")
    L.append("")
    L.append(para(adr["decision"]))
    L.append("")
    L.append("## Alternatives considered")
    L.append("")
    for name, why in adr["alternatives"]:
        L.append(f"- **{name}.** {para(why).replace(chr(10), ' ')}")
    L.append("")
    L.append("## Consequences")
    L.append("")
    L.append(para(adr["consequences"]))
    L.append("")
    return "\n".join(L)


def main() -> int:
    load()  # validate the model loads even though ADRs are not derived from it
    written = []
    for adr in ADRS:
        stem = f"adr-{adr['num']:03d}-{adr['slug']}"
        written.append(rel(write("adr", stem, _render(adr))))
    print(f"wrote {len(written)} ADRs:")
    for w in written:
        print(f"  {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
