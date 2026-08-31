-- Phase 2 of the feature-completion loop: makes the audit trail durable
-- across re-runs. Every pipeline run mints a fresh batch_id and tags every
-- row it creates with it, so re-running the pipeline is additive (old
-- batches' data persists) instead of destructive (schema.init_db(reset=True)
-- deleting everything). See generate_data.py::populate() and
-- schema.get_current_batch_id() for how batch_id is minted/read.
CREATE TABLE IF NOT EXISTS batches (
    batch_id       TEXT PRIMARY KEY,
    created_at     TEXT NOT NULL,
    record_counts  TEXT NOT NULL  -- JSON: {"transactions": N, "receivables": N, "checkout_abandonments": N}
);

ALTER TABLE transactions ADD COLUMN batch_id TEXT;
ALTER TABLE receivables ADD COLUMN batch_id TEXT;
ALTER TABLE checkout_abandonments ADD COLUMN batch_id TEXT;
ALTER TABLE diagnoses ADD COLUMN batch_id TEXT;
ALTER TABLE decisions ADD COLUMN batch_id TEXT;
ALTER TABLE audit_log ADD COLUMN batch_id TEXT;

-- transactions_snapshot, receivables_snapshot, checkout_abandonments_snapshot,
-- and baseline_results are intentionally NOT given a batch_id column — they
-- are current-run-only scratch tables (wiped and refilled every run), not
-- durable history. See the schema.py comment above transactions_snapshot.
