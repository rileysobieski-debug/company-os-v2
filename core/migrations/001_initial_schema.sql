-- Migration 001: initial per-tenant schema
--
-- Applied to every tenant DB on first provision. Subsequent migrations
-- layer on top; this one defines the minimum surface the Walls layer
-- expects to find.
--
-- The schema_meta table is already created by SQLiteDevAdapter on
-- tenant provisioning, so we only insert/update rows here, not create
-- the table.

-- Decisions: every governance-relevant founder-initiated action.
-- Mirrors the Phase 1 table shape so the Phase 1 -> v2 data migration
-- script (scripts/migrate_phase1_data.py, Weeks 2-3) is a straight
-- INSERT mapping.
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decided_at TEXT NOT NULL,
    source TEXT NOT NULL,
    action_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    citation_hash TEXT,
    source_citations_json TEXT,
    job_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_decisions_decided_at ON decisions(decided_at);
CREATE INDEX IF NOT EXISTS idx_decisions_action_type ON decisions(action_type);
CREATE INDEX IF NOT EXISTS idx_decisions_job_id ON decisions(job_id);

-- Trust snapshots: aggregate agent trust scores with the 60s staleness
-- guard from Week 1. (tenant, agent, computed_at) is the natural key.
CREATE TABLE IF NOT EXISTS trust_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent TEXT NOT NULL,
    computed_at TEXT NOT NULL,
    score REAL NOT NULL,
    confidence REAL NOT NULL,
    sample_count INTEGER NOT NULL,
    interval_low REAL,
    interval_high REAL,
    UNIQUE(agent, computed_at)
);

CREATE INDEX IF NOT EXISTS idx_trust_agent ON trust_snapshots(agent);
CREATE INDEX IF NOT EXISTS idx_trust_computed_at ON trust_snapshots(computed_at);

-- Dead-letter queue: governance writes that could not hit the primary
-- store. Drained by a background worker; app refuses to serve until
-- backlog is zero. Full DLQ shape lands in Weeks 4-5 Memory layer;
-- this migration creates the table so the adapter contract is
-- complete ahead of that work.
CREATE TABLE IF NOT EXISTS governance_dlq (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    enqueued_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    last_error TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    drained_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_dlq_drained_at ON governance_dlq(drained_at);

-- Inherited context: transition-mode tenants import existing state
-- from Notion / QuickBooks / Slack via `core.import_adapters`. Imports
-- land as hardened=FALSE Shadow Context and graduate to hardened=TRUE
-- via the 14-day decay worker or explicit founder hardening. Only
-- hardened rows feed the evaluator's hard-constraint gate.
CREATE TABLE IF NOT EXISTS inherited_context (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    imported_at TEXT NOT NULL,
    source_adapter TEXT NOT NULL,
    source_entity_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    hardened INTEGER NOT NULL DEFAULT 0,
    hardened_at TEXT,
    UNIQUE(source_adapter, source_entity_id)
);

CREATE INDEX IF NOT EXISTS idx_inherited_hardened ON inherited_context(hardened);
CREATE INDEX IF NOT EXISTS idx_inherited_source ON inherited_context(source_adapter);
