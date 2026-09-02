-- Phase 4 of the feature-completion loop: gives an escalate_to_human
-- decision an actual operational surface. Before this, execution.py just
-- logged "Ticket logged for human review — no automated outcome." with no
-- ticket object anywhere a human could act on.
--
-- One row per escalated RECORD (record_id is UNIQUE — a re-escalation
-- upserts rather than duplicates; see schema.upsert_escalation()).
-- Deliberately NOT batch-scoped by default in the API — this is a durable,
-- cross-run worklist, unlike most of the rest of the dashboard which
-- defaults to the latest batch only.
CREATE TABLE IF NOT EXISTS escalations (
    escalation_id   TEXT PRIMARY KEY,
    record_id       TEXT NOT NULL UNIQUE,
    record_type     TEXT NOT NULL,
    reason          TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open',
    resolved_at     TEXT,
    resolver_note   TEXT,
    batch_id        TEXT
);
