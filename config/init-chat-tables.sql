-- Chat mediator persistence: sessions and messages.
--
-- Reviewable, versioned DDL for a human to run once (same spirit as init-sentinel-db.sql), and
-- textually identical to CHAT_DDL in sentinel/chat/db.py, which sentinel/chat/api.py runs itself
-- at startup — that in-code copy is what makes a fresh dev machine work without remembering to
-- run this file, but this file is what makes the schema reviewable as a diff. Keep the two in
-- sync by hand.
--
-- Runs against the `sentinel` database (owned by sentinelapp) — chat sessions are Sentinel-owned
-- persistence, not otel-demo's, per CLAUDE.md's infra-layer section.
--
-- Against the `public` schema — this file is for a human applying the DDL by hand against real,
-- production-shaped state. When `settings.debug` is true, `sentinel/chat/db.py` creates and uses a
-- separate `test` schema instead (via `search_path`), so locally-tested chat sessions never show
-- up next to real ones; that schema is disposable and has no checked-in DDL file of its own.

CREATE TABLE IF NOT EXISTS chat_sessions (
    session_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        TEXT NOT NULL DEFAULT 'default',
    role             TEXT NOT NULL,              -- viewer|oncall|sre|admin, fixed for the session
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    context_summary  TEXT,                        -- rolling summary of turns older than the raw window
    summary_upto_seq BIGINT NOT NULL DEFAULT 0,    -- highest chat_messages.seq folded into context_summary
    title            TEXT                          -- first user message, truncated; sidebar label
);

CREATE TABLE IF NOT EXISTS chat_messages (
    message_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id    UUID NOT NULL REFERENCES chat_sessions(session_id) ON DELETE CASCADE,
    seq           BIGINT NOT NULL,               -- monotonic per-session ordering, not wall-clock
    turn_role     TEXT NOT NULL,                  -- 'user' | 'assistant'
    content       TEXT NOT NULL,
    route         TEXT,                           -- 'tool_call' | 'rag' | 'rca' | 'refuse' | NULL (user turns)
    rca_thread_id TEXT,                           -- set when route='rca'; joins LangGraph's own checkpoint tables
    rca_status    TEXT,                           -- 'running' | 'awaiting_approval' | 'published' | 'rejected' | NULL
    latency_ms    INTEGER,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    meta          JSONB NOT NULL DEFAULT '{}'::jsonb  -- refs cited, tool called, degraded flag, etc.
);
CREATE UNIQUE INDEX IF NOT EXISTS chat_messages_session_seq_uidx ON chat_messages (session_id, seq);
