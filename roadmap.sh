#!/usr/bin/env bash
# roadmap.sh — session-by-session curriculum for learning sentinel-rag by rebuilding it yourself.
#
# Two tracks, run in parallel:
#   Track A — the sentinel-rag pipeline and agent stack itself (16 sessions)
#   Track B — a real dependency behind it: OpenTelemetry Demo as the mock platform,
#             a custom parent->child health-rollup service, rca/clients.py wired to
#             something real, and a chat UI in front of the RCA API (8 sessions)
#
# Sessions assume ~3 hours each. Progress is tracked locally in .roadmap-state
# (gitignored — this is your personal progress, not project state).
#
# Usage:
#   ./roadmap.sh                 overview of both tracks
#   ./roadmap.sh show a3         full detail for one session
#   ./roadmap.sh next            next unfinished session per track
#   ./roadmap.sh done a3         mark a session complete
#   ./roadmap.sh undone a3       unmark it
#   ./roadmap.sh status          progress bars
#   ./roadmap.sh reset           clear all progress

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_FILE="$ROOT/.archive/.roadmap-state"

IDS_A=(a1 a2 a3 a4 a5 a6 a7 a8 a9 a10 a11 a12 a13 a14 a15 a16)
IDS_B=(b1 b2 b3 b4 b5 b6 b7 b8)

declare -A TITLE=(
  [a1]="Orientation & environment"
  [a2]="Config, observability, resilience, cache"
  [a3]="Parsing: local adapter + quality scoring"
  [a4]="Parsing: hybrid routing + cloud escalation"
  [a5]="Chunking strategies"
  [a6]="Embedding & the query/document asymmetry trap"
  [a7]="Vectorstore + ingestion pipeline"
  [a8]="Security: ACL filtering"
  [a9]="Retrieval pipeline & fusion"
  [a10]="Reranking & the relevance floor"
  [a11]="Generation & mechanical grounding"
  [a12]="Evaluation & the CI gate"
  [a13]="RCA agent graph topology"
  [a14]="RCA tools, resilience, and the platform stub"
  [a15]="Human-in-the-loop: writer, verifier, approval"
  [a16]="Capstone: design decisions + self-test"
  [b1]="Stand up the OpenTelemetry Demo"
  [b2]="Fault injection & real traffic"
  [b3]="Build the parent->child health-rollup service"
  [b4]="Wire rca/clients.py to the real thing"
  [b5]="Mock user + incident scenarios"
  [b6]="Chat UI: clone and understand the protocol gap"
  [b7]="Wire the chat UI to sentinel"
  [b8]="One pane of glass"
)

# ---------------------------------------------------------------- session detail
detail() {
  case "$1" in
  a1) cat <<'EOF'
READ
  README.md
  docs/architecture.md
  sentinel/core/types.py         (the Chunk two-field contract)
  sentinel/core/registry.py

DO
  make install
  make up
  make doctor
  # skim the whole corpus, it's only 5 files
  cat knowledge/*/*.md

CHECKPOINT
  Why does Chunk carry embed_text and context_text separately instead of one
  field? What breaks if embed_chunks embeds context_text instead?

DELIVERABLE
  `make doctor` output showing the local extras green.
EOF
  ;;
  a2) cat <<'EOF'
READ
  sentinel/core/config.py        (index_version fingerprint, _check_provider_prerequisites)
  sentinel/core/observability.py
  sentinel/core/resilience.py    (guarded(), circuit breaker)
  sentinel/core/cache.py         (LIVE_TOOLS)

DO
  python -c "from sentinel.core.config import settings; print(settings.index_version)"
  # deliberately select a provider without its credential in .env and watch
  # the fail-fast validator reject it at startup, not 20 minutes into an ingest
  grep -rn LIVE_TOOLS sentinel/

CHECKPOINT
  Name one tool that must never be cached, and say why a cached answer there
  is worse than no answer at all.

DELIVERABLE
  One paragraph on what index_version fingerprints, and why a mismatch
  returns nothing instead of subtly wrong results (docs/runbook.md, "Everything
  got worse after a deploy").
EOF
  ;;
  a3) cat <<'EOF'
READ
  sentinel/parsing/base.py
  sentinel/parsing/local_parser.py
  sentinel/parsing/quality.py
  docs/STAGE-OVERVIEW.md  section 1 (Parsing)

DO
  python scripts/parsing/01_parse_local.py knowledge/methodology/index-calculation.md
  # run it against every doc in knowledge/, then drop in a multi-column or
  # scanned document of your own and watch the quality score drop

CHECKPOINT
  What does the heuristic quality score actually detect, and what does it
  systematically miss? (STAGE-OVERVIEW: "Why the quality score is heuristic")

DELIVERABLE
  Quality scores for 3+ documents, including one you picked to be bad.
EOF
  ;;
  a4) cat <<'EOF'
READ
  sentinel/parsing/hybrid.py     (ADR-004 in docs/design-decisions.md)
  sentinel/parsing/azure_parser.py, aws_parser.py  (skim — shape, not detail)
  docs/adapters.md               section "Parsing"

DO
  python scripts/parsing/04_hybrid_compare.py
  # if you have Azure Document Intelligence / AWS Textract trial credentials,
  # run 02_parse_azure.py and 03_parse_aws.py too, and check what you actually
  # see against adapters.md's differences table

CHECKPOINT
  Which formats never leave the local parser, and why is that a routing
  decision rather than a quality decision?

DELIVERABLE
  hybrid_compare output, plus a note on local-only fallback behaviour without
  cloud credentials.
EOF
  ;;
  a5) cat <<'EOF'
READ
  sentinel/chunking/base.py
  sentinel/chunking/strategies.py   (all nine strategies)
  docs/design-decisions.md  ADR-007, ADR-008, ADR-009

DO
  python benchmarks/chunking_sweep.py
  # compare `structural` vs `semantic` vs `runbook` specifically on
  # knowledge/runbook/ice-restart.md

CHECKPOINT
  Why does `runbook` exist as its own strategy instead of reusing `structural`?

DELIVERABLE
  chunking_sweep CSV, plus your own doc-type routing decision written down
  for a document type not yet in the routing table.
EOF
  ;;
  a6) cat <<'EOF'
READ
  sentinel/embedding/base.py
  sentinel/embedding/local_embedder.py
  docs/design-decisions.md  ADR-003
  docs/STAGE-OVERVIEW.md  section 3 (Embedding)

DO
  python scripts/embedding/01_embed_local.py
  # deliberately reproduce the query/document asymmetry trap it demonstrates:
  # embed a query with the document-side method (or vice versa) and compare

CHECKPOINT
  On a model that distinguishes query vs document embeddings, what actually
  goes wrong if you get the two swapped? Is it a crash or a silent quality loss?

DELIVERABLE
  A before/after showing the asymmetry bug and the fix.
EOF
  ;;
  a7) cat <<'EOF'
READ
  sentinel/vectorstore/base.py
  sentinel/vectorstore/pgvector_store.py
  sentinel/ingest/pipeline.py
  Chunk.make_id  in sentinel/core/types.py

DO
  make ingest
  make ingest              # run again unchanged — confirm zero re-embeds
  # edit one paragraph of a knowledge/ doc, re-ingest, confirm only that
  # chunk's embedding is recomputed, not the whole document

CHECKPOINT
  Why are chunk ids content-derived rather than positional, and why does
  that matter for citations surviving re-ingestion?

DELIVERABLE
  Log evidence of the idempotent re-ingest: chunk counts touched before and
  after the one-paragraph edit.
EOF
  ;;
  a8) cat <<'EOF'
READ
  SearchFilter.require_scope()  in sentinel/vectorstore/base.py
  sentinel/ingest/acl.py
  tests/test_acl.py
  docs/design-decisions.md  ADR-005, ADR-006

DO
  python scripts/vectorstore/02_acl_filtering.py
  pytest tests/test_acl.py -v
  # by hand: run golden g010 (role=admin, expects an answer) then g011
  # (role=support, same question, expects a refusal) and compare
  # as an exercise, sketch (don't ship) what moving the filter to a
  # post-query step would let leak through, per ADR-006

CHECKPOINT
  Why must a refusal never disclose that the restricted document exists, and
  where exactly in the code is that enforced?

DELIVERABLE
  All-green `pytest tests/test_acl.py -v`, plus one new adversarial case you
  wrote yourself added to that file.
EOF
  ;;
  a9) cat <<'EOF'
READ
  sentinel/retrieval/pipeline.py
  sentinel/retrieval/fusion.py       (RRF, MMR, dedupe)
  sentinel/retrieval/query_transform.py
  docs/design-decisions.md  ADR-010

DO
  python benchmarks/retrieval_sweep.py
  # by hand, on 2-3 golden questions, compare RRF's ranking against a naive
  # weighted-score blend of the same two candidate lists

CHECKPOINT
  Why RRF instead of weighted blending — what does blending get wrong when
  the two scorers' distributions don't overlap?

DELIVERABLE
  retrieval_sweep output plus your written answer to the checkpoint.
EOF
  ;;
  a10) cat <<'EOF'
READ
  sentinel/reranking/base.py
  sentinel/reranking/local_reranker.py
  docs/STAGE-OVERVIEW.md  section 6, "Why the relevance floor is not optional"

DO
  python scripts/reranking/01_rerank_compare.py
  # find min_rerank_score in config, deliberately miscalibrate it, rerun
  # retrieval, and reproduce docs/runbook.md's `returned: 0` symptom

CHECKPOINT
  Why is the relevance floor not portable between different rerankers?

DELIVERABLE
  rerank_compare output, before and after the floor miscalibration.
EOF
  ;;
  a11) cat <<'EOF'
READ
  sentinel/generation/base.py
  sentinel/generation/local_llm.py
  rca/agents/common.observed_refs  in sentinel/rca/agents/common.py
  docs/STAGE-OVERVIEW.md  sections 7 and 9 ("Grounding is mechanical, not judged")

DO
  python scripts/llm/01_generate_grounded.py
  python scripts/llm/02_structured_and_tools.py
  # try to get a citation to a ref that never appeared in a real tool result
  # into the output (a mini prompt-injection exercise), and confirm
  # observed_refs drops it rather than the verifier arguing about it

CHECKPOINT
  Why is grounding a set-membership test enforced in code, rather than a
  question posed to the verifier model?

DELIVERABLE
  A captured example of a fabricated citation being silently dropped.
EOF
  ;;
  a12) cat <<'EOF'
READ
  sentinel/evaluation/metrics.py, retrieval_eval.py, generation_eval.py,
  golden.py, gate.py
  docs/design-decisions.md  ADR-019 through ADR-023

DO
  make eval
  make eval-full
  # read every category in data/golden.jsonl: howto, terminology, lookup,
  # design_rationale, multi_hop, acl_denied, unanswerable, live_status, mixed
  # then, per CLAUDE.md: change a chunking parameter, run `make eval` before
  # and after, and judge whether any metric move is signal or noise on a
  # 40-example golden set

CHECKPOINT
  Why must safety checks (ACL, refusal) never be averaged into an aggregate
  score alongside quality metrics?

DELIVERABLE
  Before/after eval numbers, with your own note on whether the change is
  real or within noise.
EOF
  ;;
  a13) cat <<'EOF'
READ
  sentinel/rca/graph.py
  sentinel/rca/state.py            (the Annotated reducers)
  sentinel/rca/agents/supervisor.py, specialists.py
  sentinel/rca/prompts.py
  tests/test_trajectory.py

DO
  pytest tests/test_trajectory.py -v
  # by hand, trace which specialists fire concurrently for golden question
  # g017 (category "mixed", expects both search_knowledge and feed_status)

CHECKPOINT
  Why must the supervisor hold zero tools by construction, and what does
  test_trajectory.py actually assert to keep that true over time?

DELIVERABLE
  A written (or drawn) trace of g017's specialist fan-out.
EOF
  ;;
  a14) cat <<'EOF'
READ
  sentinel/rca/tools/retrieval.py, change.py, warehouse.py
    (sqlglot parsing + forced LIMIT + read-only DB role — ADR-018)
  sentinel/core/resilience.py      (guarded())
  sentinel/rca/clients.py          (every NotImplementedError — this is the
                                     seam Track B fills in)

DO
  pytest -k warehouse
  # feed run_readonly_sql a multi-statement string, then a write attempt,
  # and confirm sqlglot rejects both before the DB-level read-only role
  # would even need to catch it

CHECKPOINT
  Name the four layers of SQL safety. Which one is the actual guarantee, and
  which are defense in depth on top of it?

DELIVERABLE
  A rejected injection attempt and its log line.
EOF
  ;;
  a15) cat <<'EOF'
READ
  sentinel/rca/agents/writer.py    (interrupt())
  sentinel/rca/agents/verifier.py
  sentinel/rca/runtime.py          (tenant/role contextvars)
  sentinel/rca/db.py               (checkpointer)

DO
  make api
  # run the three curl commands from README.md end to end: start,
  # stream, decision — against a real golden anomaly
  # then kill and restart the API mid-interrupt and confirm the run
  # survives, because the checkpointer is Postgres
  # (docs/runbook.md, "An investigation is stuck")

CHECKPOINT
  Why must tenant and role scope come from a contextvar rather than a tool
  argument a model could write?

DELIVERABLE
  A full curl trace: start -> stream -> decision -> published outcome.
EOF
  ;;
  a16) cat <<'EOF'
READ
  Every ADR in docs/design-decisions.md not yet covered by earlier sessions
  docs/interview-guide.md  — cover the answers, answer from memory, then check

DO
  Pick one ADR you'd actually argue with and write a one-paragraph counter.
  If Track B has caught up, re-run the full investigation flow wired to the
  real mock platform instead of the stub.

CHECKPOINT
  Of CLAUDE.md's "Non-negotiable invariants," which one would be most
  dangerous to relax first, and what's the smallest change that would do it?

DELIVERABLE
  Your counter-ADR paragraph.
EOF
  ;;
  b1) cat <<'EOF'
ALREADY DONE (verify, then read)
  app/otel-demo is cloned and running: `cd app/otel-demo && make start-minimal-no-o11y`
  starts core services + load-generator + flagd, skipping Jaeger/Prometheus/
  Grafana (compose.observability.yaml) to save RAM on a small machine — that
  stack comes in session B8 instead, once you actually need it.

  Gotcha worth knowing before you touch flagd/astronomy-db config yourself:
  if /projects (or wherever you're working) is on a filesystem where chmod
  doesn't actually change the reported mode (fuseblk/drvfs-style mounts do
  this — check with `stat` before and after a chmod), postgres and flagd
  will fail with "permission denied" reading their bind-mounted config
  files, because their entrypoints drop to a non-root user internally that
  can't read a file it doesn't own at whatever mode that filesystem hands
  back. Fix in place: compose.extras.yaml overrides astronomy-db's, flagd's
  and flagd-ui's volumes to a runtime staging copy under
  ~/.local/share/sentinel-rag/runtime/ (real filesystem) instead of
  upstream's `./src/...` bind mounts. The git-tracked app/otel-demo/src/
  copies stay as shipped — they're the seed, not what containers actually
  read. If you edit demo.flagd.json or postgresql/init.sql by hand, edit the
  runtime copy (or the tracked one and re-copy it over) — see the comment
  above the `services:` block in compose.extras.yaml.

DO
  make doctor-equivalent for this stack: `docker ps -a`, confirm every core
  service shows `(healthy)`. Read app/otel-demo's own README for the full
  service list and topology diagram.

CHECKPOINT
  Map OTel-demo services to sentinel's four rca/clients.py clients: which
  service plays "reference data" (corporate actions / constituents), which
  plays "deploy history", which plays "live health"? DeployClient,
  ReferenceDataClient, MonitorClient and ITSMClient each need a real target.

DELIVERABLE
  A one-page service map, in your own words, matching OTel-demo services to
  the four clients.
EOF
  ;;
  b2) cat <<'EOF'
ALREADY DONE (verify, then use)
  app/incident-generator is running (port 8092) — it already does the
  scheduled version of this session: every 1-4 minutes it flips one of nine
  real otel-demo fault flags on for a few minutes, logs the state change,
  and flips it back off. It self-heals a stuck flag on restart (see
  reset_all_flags() in generator.py) — worth reading, it's a small, complete
  example of the "infrastructure failure" recovery pattern sentinel's own
  core/resilience.py uses at a bigger scale.
  app/system-monitor (port 8091) is your observation instrument for this
  session, since Jaeger isn't running in the no-o11y profile (that's B8).

DO
  curl localhost:8092/scenarios                    # see what's available
  curl -X POST localhost:8092/trigger/payment_degraded
  watch -n2 curl -s localhost:8091/tree             # watch the rollup react live
  curl localhost:8092/incidents                     # the ground-truth log

CHECKPOINT
  Compare what you just watched happen against sentinel's own failure model
  in docs/architecture.md — infrastructure failure (retry/breaker/degrade)
  vs semantic failure (no results/access denied). Which category does a
  flagd fault flip actually belong to, from the *caller's* point of view?

DELIVERABLE
  One `/tree` snapshot mid-fault and one after recovery, saved side by side.
EOF
  ;;
  b3) cat <<'EOF'
ALREADY DONE (read first, then extend)
  app/system-monitor is running (port 8091). Read tree.yaml before anything
  else — the dependency graph is pulled directly from otel-demo's own
  compose.yaml `depends_on` blocks, not hand-drawn, and the file explains
  what's deliberately excluded (otel-collector, flagd, load-generator) and
  why. Read monitor.py's docstring for why it reads Docker's own healthcheck
  state via the Docker API instead of reimplementing each service's health
  protocol.

DO
  docker pause payment && curl localhost:8091/nodes/checkout && curl localhost:8091/health
  docker unpause payment
  # confirm: checkout and the frontend-proxy root go unhealthy, an unrelated
  # leaf (curl localhost:8091/nodes/ad) does not.

EXTEND (the actual session — pick one)
  - own_health() currently treats a single bad poll as truth. Sentinel's own
    circuit breaker in core/resilience.py requires N consecutive failures
    before opening. Should the health tree borrow that debounce, and what
    would change if it did?
  - severity is a flat 4-state ordering (healthy/starting/missing/unhealthy).
    Is that enough to answer "how long has this been down," or does the
    monitor need to start keeping history?
  - add a node currently excluded (otel-collector or flagd) as an
    *informational* branch that doesn't affect rollup severity — argue
    first whether that's worth doing given tree.yaml's stated reasoning for
    excluding them.

DELIVERABLE
  Your chosen extension, plus the pause/unpause trace showing the rollup
  work correctly both before and after your change.
EOF
  ;;
  b4) cat <<'EOF'
DO
  Implement DeployClient, ReferenceDataClient, MonitorClient and ITSMClient
  in sentinel/rca/clients.py against your B1-B3 services, replacing each
  NotImplementedError. Keep the @guarded() decorators intact so the
  breaker/degradation behaviour documented in docs/runbook.md still applies.
  This is the "rewrite a few things in sentinel" part of the exercise.

CHECKPOINT
  Of the four clients, which one failing should degrade the investigation
  (per ADR "Degrade, never fail"), and which failing is serious enough to be
  a kill-switch conversation rather than a degraded answer?

DELIVERABLE
  A test run showing the RCA graph completing an investigation with at
  least one real (non-stubbed) tool call in the trace.
EOF
  ;;
  b5) cat <<'EOF'
DO
  Script 2-3 named scenarios: a traffic pattern plus a scheduled fault
  injection, shaped like the incident in knowledge/incident/INCIDENT-4471.md
  — even though the underlying app is an astronomy shop. The point is
  exercising MonitorClient/DeployClient code paths, not domain fidelity.

CHECKPOINT
  Decide, and argue it out before doing it: is it more valuable to keep the
  RCA agent's domain (index calculation) and the knowledge/ corpus as-is
  while only the live-tool backend changes underneath, or to also rewrite
  the corpus to match the astronomy-shop domain?

DELIVERABLE
  One scripted scenario run start to finish, with your own written incident
  narrative.
EOF
  ;;
  b6) cat <<'EOF'
DO
  git clone https://github.com/langchain-ai/agent-chat-ui ui/agent-chat-ui
  cd ui/agent-chat-ui && npm install && npm run dev
  # run it first against its OWN demo LangGraph server, before touching
  # sentinel, to see the shape it actually expects: threads, runs/stream,
  # and a `messages: BaseMessage[]` field in graph state.
  # then re-read sentinel/rca/api.py and sentinel/rca/state.py — sentinel's
  # state is evidence-shaped, not messages-shaped. This is a real gap, not
  # a plug-and-play integration.

CHECKPOINT
  Write down the tradeoff before choosing:
    (a) run `langgraph dev` on sentinel/rca/graph.py directly and extend
        state.py with a synthetic messages list, or
    (b) write a thin adapter server translating the existing FastAPI/SSE
        surface into the LangGraph SDK's wire protocol.

DELIVERABLE
  agent-chat-ui running locally against something (even a toy graph), plus
  your written decision between (a) and (b).
EOF
  ;;
  b7) cat <<'EOF'
DO
  Implement whichever adapter you chose in B6. Get a real investigation's
  node progress and final draft rendering as chat messages in
  agent-chat-ui, including the interrupt/approve step rendering as an
  actual decision point in the UI — not a dead end.

CHECKPOINT
  What's lost translating sentinel's structured evidence/draft state into a
  chat message stream? Is anything about grounding or citation traceability
  at risk of getting flattened in the process?

DELIVERABLE
  A screen recording or screenshot sequence: trigger anomaly -> chat shows
  agent trajectory -> approval UI -> published.
EOF
  ;;
  b8) cat <<'EOF'
DO
  Sessions B1-B7 deliberately ran on the no-o11y profile to save RAM —
  Jaeger/Prometheus/Grafana were never started. Bring them in now:
    cd app/otel-demo && make start   # full profile, includes compose.observability.yaml
  Watch memory while it comes up (`free -h`) — if it's tight, this is the
  moment to stop non-essential containers (sentinel's own `make up` stack,
  if it's running) rather than let the OOM killer choose for you.
  Then put sentinel's own metrics (sentinel_cost_usd, sentinel_degraded_total,
  per-stage retrieval latency) onto the same Grafana instance, alongside the
  platform's own dashboards.

CHECKPOINT
  Using docs/runbook.md's "Useful queries" section, build one Grafana panel
  per query and confirm each shows real (non-zero) data from your Track A
  and Track B work.

DELIVERABLE
  A saved Grafana dashboard with at least 4 working panels from that list.
EOF
  ;;
  *) echo "unknown session id: $1" >&2; exit 1 ;;
  esac
}

# ---------------------------------------------------------------- state
touch "$STATE_FILE"

is_done() { grep -qxF "$1" "$STATE_FILE" 2>/dev/null; }

mark_done() {
  is_done "$1" || echo "$1" >> "$STATE_FILE"
  echo "marked $1 done: ${TITLE[$1]:-unknown}"
}

mark_undone() {
  sed -i.bak "/^$1\$/d" "$STATE_FILE" && rm -f "$STATE_FILE.bak"
  echo "marked $1 not done: ${TITLE[$1]:-unknown}"
}

check() { is_done "$1" && echo "[x]" || echo "[ ]"; }

# ---------------------------------------------------------------- rendering
print_track() {
  local label="$1"; shift
  local ids=("$@")
  local done_n=0
  echo "== $label =="
  for id in "${ids[@]}"; do
    is_done "$id" && done_n=$((done_n + 1))
    printf "  %s %-4s %s\n" "$(check "$id")" "$id" "${TITLE[$id]}"
  done
  echo "  -> ${done_n}/${#ids[@]} sessions done, $(( (${#ids[@]} - done_n) * 3 ))h remaining"
  echo
}

overview() {
  echo "sentinel-rag roadmap — ${#IDS_A[@]} + ${#IDS_B[@]} sessions, ~3h each, two parallel tracks"
  echo
  print_track "Track A — the pipeline & agents (sentinel-rag itself)" "${IDS_A[@]}"
  print_track "Track B — mock platform + chat UI" "${IDS_B[@]}"
  echo "commands: show <id> | next | done <id> | undone <id> | status | reset"
}

show_next() {
  for label_ids in "Track A:${IDS_A[*]}" "Track B:${IDS_B[*]}"; do
    local label="${label_ids%%:*}"
    local ids=(${label_ids#*:})
    local found=""
    for id in "${ids[@]}"; do
      if ! is_done "$id"; then found="$id"; break; fi
    done
    if [ -n "$found" ]; then
      echo "$label next: $found — ${TITLE[$found]}"
    else
      echo "$label: all sessions done"
    fi
  done
}

status() {
  bar() {
    local done_n=$1 total=$2 width=30
    local filled=$(( done_n * width / total ))
    printf "["
    for ((i=0; i<width; i++)); do [ "$i" -lt "$filled" ] && printf "#" || printf "."; done
    printf "] %d/%d\n" "$done_n" "$total"
  }
  local da=0 db=0
  for id in "${IDS_A[@]}"; do is_done "$id" && da=$((da+1)); done
  for id in "${IDS_B[@]}"; do is_done "$id" && db=$((db+1)); done
  echo -n "Track A "; bar "$da" "${#IDS_A[@]}"
  echo -n "Track B "; bar "$db" "${#IDS_B[@]}"
  echo -n "Overall "; bar "$((da+db))" "$(( ${#IDS_A[@]} + ${#IDS_B[@]} ))"
}

# ---------------------------------------------------------------- main
cmd="${1:-overview}"
case "$cmd" in
  overview|"") overview ;;
  show) [ -n "${2:-}" ] || { echo "usage: $0 show <id>" >&2; exit 1; }
        echo "== ${2}: ${TITLE[$2]:-unknown} (~3h) =="
        echo
        detail "$2" ;;
  next) show_next ;;
  done) [ -n "${2:-}" ] || { echo "usage: $0 done <id>" >&2; exit 1; }; mark_done "$2" ;;
  undone) [ -n "${2:-}" ] || { echo "usage: $0 undone <id>" >&2; exit 1; }; mark_undone "$2" ;;
  status) status ;;
  reset) > "$STATE_FILE"; echo "progress reset" ;;
  help|-h|--help)
    echo "usage: $0 [overview|show <id>|next|done <id>|undone <id>|status|reset]" ;;
  *) echo "unknown command: $cmd" >&2; exit 1 ;;
esac
