"""
Schema definitions for Reclaim (AI Revenue Recovery Agent).
SQLite chosen over JSON because:
  - We need queryable filters (status, failure_code) for the dashboard and audit trail
  - Atomic updates per transaction as the pipeline moves it through stages
  - Easy to inspect with any SQLite browser for judges/debugging
"""

import sqlite3

from config import DB_PATH

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS batches (
    batch_id       TEXT PRIMARY KEY,
    created_at     TEXT NOT NULL,
    record_counts  TEXT NOT NULL  -- JSON: {"transactions": N, "receivables": N, "checkout_abandonments": N}
);

CREATE TABLE IF NOT EXISTS transactions (
    transaction_id      TEXT PRIMARY KEY,
    customer_id         TEXT NOT NULL,
    amount              REAL NOT NULL,
    currency             TEXT NOT NULL DEFAULT 'INR',
    payment_method       TEXT NOT NULL,          -- card / UPI / netbanking / mandate
    failure_code          TEXT NOT NULL,          -- insufficient_funds / expired_card / bank_timeout / otp_failed / risk_block / issuer_decline
    failure_timestamp     TEXT NOT NULL,
    attempt_count          INTEGER NOT NULL DEFAULT 0,
    status                TEXT NOT NULL DEFAULT 'open',  -- open / recovering / recovered / lost / escalated / stopped
    customer_opt_out       INTEGER NOT NULL DEFAULT 0,     -- 0/1
    last_action_timestamp  TEXT,                            -- used to enforce 24h cool-off
    batch_id                TEXT                            -- which pipeline run generated this record; see migrations/0003_batch_id.sql
);

CREATE TABLE IF NOT EXISTS receivables (
    invoice_id       TEXT PRIMARY KEY,
    customer_id      TEXT NOT NULL,
    amount           REAL NOT NULL,
    due_date         TEXT NOT NULL,
    days_overdue     INTEGER NOT NULL,
    contact_history  TEXT NOT NULL DEFAULT '[]',   -- JSON list of prior contact events
    status           TEXT NOT NULL DEFAULT 'open',  -- open / recovering / recovered / lost / escalated / stopped
    customer_opt_out       INTEGER NOT NULL DEFAULT 0,
    attempt_count           INTEGER NOT NULL DEFAULT 0,
    last_action_timestamp   TEXT,
    promised_pay_date        TEXT,  -- nullable date the customer promised payment by; NULL if no promise on file.
                                     -- See migrations/0004_promise_to_pay.sql.
    promise_status            TEXT NOT NULL DEFAULT 'none',  -- none / pending / broken
    batch_id                 TEXT
);

CREATE TABLE IF NOT EXISTS checkout_abandonments (
    session_id           TEXT PRIMARY KEY,
    customer_id          TEXT NOT NULL,
    cart_value           REAL NOT NULL,
    abandoned_at         TEXT NOT NULL,
    payment_attempted    INTEGER NOT NULL DEFAULT 0,  -- 0 = never started payment, 1 = attempted then left
    customer_opt_out     INTEGER NOT NULL DEFAULT 0,
    attempt_count        INTEGER NOT NULL DEFAULT 0,
    last_action_timestamp TEXT,
    status               TEXT NOT NULL DEFAULT 'open',  -- open / recovering / recovered / lost / escalated / stopped
    recovered_amount     REAL,  -- true net amount recovered (cart_value, or cart_value*(1-DISCOUNT_PCT) for a
                                 -- discount-nudge recovery); NULL until recovered. See migrations/0002_recovered_amount.sql.
    batch_id              TEXT
);

CREATE TABLE IF NOT EXISTS checkout_abandonments_snapshot (
    session_id TEXT PRIMARY KEY, customer_id TEXT, cart_value REAL, abandoned_at TEXT,
    payment_attempted INTEGER, customer_opt_out INTEGER, attempt_count INTEGER,
    last_action_timestamp TEXT, status TEXT
);

CREATE TABLE IF NOT EXISTS diagnoses (
    record_id       TEXT PRIMARY KEY,   -- transaction_id or invoice_id
    record_type     TEXT NOT NULL,      -- 'transaction' or 'receivable'
    root_cause      TEXT NOT NULL,
    confidence      REAL NOT NULL,
    risk_flag       INTEGER NOT NULL DEFAULT 0,   -- blocks ALL automated action, routes to human (fraud/risk only)
    needs_manual_followup INTEGER NOT NULL DEFAULT 0,  -- flags for a human's attention WITHOUT blocking automated outreach
    recommended_urgency TEXT NOT NULL,  -- immediate / within_24h / within_week
    diagnosed_at    TEXT NOT NULL,
    batch_id         TEXT
);

CREATE TABLE IF NOT EXISTS decisions (
    decision_id     TEXT PRIMARY KEY,
    record_id       TEXT NOT NULL,
    record_type     TEXT NOT NULL,
    attempt_number  INTEGER NOT NULL DEFAULT 1,   -- which agent-loop attempt this decision belongs to
    action          TEXT NOT NULL,      -- retry_payment / send_update_link / send_reminder / escalate_reminder / escalate_to_human / stop_no_action
    reasoning       TEXT NOT NULL,
    retry_at        TEXT,
    decided_at      TEXT NOT NULL,
    stopping_rule_fired TEXT,           -- name of rule if one triggered this decision, else NULL
    candidate_actions   TEXT,           -- JSON list of {candidate_action, probability, expected_value} for every ML-scored option considered, incl. the one chosen
    ml_selected_action  TEXT,           -- the granular candidate action the ML/EV comparison picked (e.g. RETRY_LATER), distinct from the broad `action` execution category
    batch_id             TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    log_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id       TEXT NOT NULL,
    record_type     TEXT NOT NULL,
    timestamp       TEXT NOT NULL,
    event_type      TEXT NOT NULL DEFAULT 'ACTION_EXECUTED',  -- ACTION_EXECUTED / RECOVERY_SUCCESS / REPLANNED / ESCALATED / STOPPED / POLICY_BLOCKED
    action_taken    TEXT NOT NULL,
    reasoning       TEXT NOT NULL,
    outcome         TEXT NOT NULL,
    actor           TEXT NOT NULL,       -- 'agent' or 'human'
    batch_id         TEXT
);

-- Pristine copies of transactions/receivables taken immediately after data
-- generation, before either the agent pipeline or the baseline strategy
-- mutates anything. Both are scored against this identical starting point so
-- baseline-vs-agent is a fair, apples-to-apples comparison.
--
-- NOT batch-tagged (Phase 2): these are deliberately wiped and refilled with
-- ONLY the current/latest batch's pristine rows on every run (see
-- schema.snapshot_initial_data) — they exist solely to give the baseline
-- strategy an untouched starting point for the CURRENT run's comparison, not
-- as durable history. Durable history lives in the batch-tagged tables
-- above. baseline_results (further below) follows the same non-batch-tagged,
-- current-run-only convention, for the same reason.
CREATE TABLE IF NOT EXISTS transactions_snapshot (
    transaction_id TEXT PRIMARY KEY, customer_id TEXT, amount REAL, currency TEXT,
    payment_method TEXT, failure_code TEXT, failure_timestamp TEXT,
    attempt_count INTEGER, status TEXT, customer_opt_out INTEGER, last_action_timestamp TEXT
);

CREATE TABLE IF NOT EXISTS receivables_snapshot (
    invoice_id TEXT PRIMARY KEY, customer_id TEXT, amount REAL, due_date TEXT,
    days_overdue INTEGER, contact_history TEXT, status TEXT, customer_opt_out INTEGER,
    attempt_count INTEGER, last_action_timestamp TEXT
);

CREATE TABLE IF NOT EXISTS baseline_results (
    record_id       TEXT PRIMARY KEY,
    record_type     TEXT NOT NULL,
    amount          REAL NOT NULL,
    baseline_action TEXT NOT NULL,
    baseline_outcome TEXT NOT NULL,
    baseline_recovered INTEGER NOT NULL DEFAULT 0
);

-- Phase 4: gives an escalate_to_human decision an actual operational
-- surface — before this, escalation was just a status string with nothing
-- a human could act on. One row per escalated RECORD (not per decision —
-- record_id is UNIQUE so a re-escalation upserts rather than duplicates;
-- see schema.upsert_escalation()). Deliberately NOT scoped to "current
-- batch only" like most of the dashboard — this is a durable, cross-run
-- worklist a human works through regardless of which batch created each
-- item, so GET /api/escalations shows open items from every batch by
-- default (batch_id is kept for traceability/filtering, not as a default
-- scope).
CREATE TABLE IF NOT EXISTS escalations (
    escalation_id   TEXT PRIMARY KEY,
    record_id       TEXT NOT NULL UNIQUE,
    record_type     TEXT NOT NULL,
    reason          TEXT NOT NULL,   -- the stopping_rule_fired value that caused the escalation
    created_at      TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open',  -- open / in_review / resolved
    resolved_at     TEXT,
    resolver_note   TEXT,
    batch_id        TEXT
);
"""


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(reset: bool = False):
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if reset and DB_PATH.exists():
        DB_PATH.unlink()
    conn = get_connection()
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    conn.close()
    print(f"DB initialized at {DB_PATH}")


def upsert_escalation(conn, record_id: str, record_type: str, reason: str, batch_id, now: str):
    """Phase 4: called wherever an escalate_to_human decision is persisted
    (agent_loop.py for transactions, decision.py for receivables/
    abandonments). Upserts keyed on record_id (UNIQUE) rather than always
    inserting — if a record somehow escalates again (re-run, re-plan),
    this REOPENS it (status back to 'open', resolved_at/resolver_note
    cleared) with the latest reason/timestamp, rather than silently
    duplicating rows for the same record in the worklist."""
    import uuid as _uuid
    conn.execute(
        """INSERT INTO escalations (escalation_id, record_id, record_type, reason, created_at, status, batch_id)
           VALUES (?, ?, ?, ?, ?, 'open', ?)
           ON CONFLICT(record_id) DO UPDATE SET
               reason = excluded.reason,
               created_at = excluded.created_at,
               status = 'open',
               resolved_at = NULL,
               resolver_note = NULL,
               batch_id = excluded.batch_id""",
        (f"esc_{_uuid.uuid4().hex[:10]}", record_id, record_type, reason, now, batch_id),
    )


def create_batch(conn, record_counts: dict) -> str:
    """Phase 2: mints a fresh batch_id for one pipeline run and records it in
    the durable `batches` table. Callers tag every row they insert for this
    run (transactions/receivables/checkout_abandonments, and downstream every
    diagnosis/decision/audit_log row derived from them) with this same ID —
    see generate_data.py::populate() and get_current_batch_id() below."""
    import json as _json
    import uuid as _uuid
    from datetime import datetime as _datetime

    batch_id = f"batch_{_uuid.uuid4().hex[:12]}"
    conn.execute(
        "INSERT INTO batches (batch_id, created_at, record_counts) VALUES (?, ?, ?)",
        (batch_id, _datetime.now().isoformat(timespec="seconds"), _json.dumps(record_counts)),
    )
    return batch_id


def get_current_batch_id(conn) -> str | None:
    """The latest batch by created_at — this is what every pipeline stage
    (diagnosis/decision/agent_loop/execution) operates on by default, and
    what the live dashboard shows with no explicit batch_id param. Returns
    None if no batch has ever been created (e.g. a brand new empty DB)."""
    row = conn.execute("SELECT batch_id FROM batches ORDER BY created_at DESC, rowid DESC LIMIT 1").fetchone()
    return row["batch_id"] if row else None


def list_batches(conn) -> list[dict]:
    """All batches, newest first, with their JSON record_counts decoded —
    backs GET /api/batches."""
    import json as _json
    rows = conn.execute(
        "SELECT batch_id, created_at, record_counts FROM batches ORDER BY created_at DESC, rowid DESC"
    ).fetchall()
    return [
        {"batch_id": r["batch_id"], "created_at": r["created_at"], "record_counts": _json.loads(r["record_counts"])}
        for r in rows
    ]


def snapshot_initial_data():
    """Freezes the current transactions/receivables state into the snapshot
    tables. Must be called right after data generation, before the agent
    pipeline or baseline strategy mutate anything — see schema comment above
    transactions_snapshot for why this exists.

    Phase 2: the snapshot itself stays non-batch-tagged (current-run-only, by
    design — see the schema comment), but is now filtered to ONLY the
    current/latest batch's rows when reading from the now-cumulative source
    tables, so a re-run's snapshot doesn't pick up prior batches' rows too."""
    conn = get_connection()
    cur = conn.cursor()
    current_batch = get_current_batch_id(conn)
    cur.execute("DELETE FROM transactions_snapshot")
    cur.execute("DELETE FROM receivables_snapshot")
    cur.execute("DELETE FROM checkout_abandonments_snapshot")
    cur.execute(
        """INSERT INTO transactions_snapshot
           SELECT transaction_id, customer_id, amount, currency, payment_method, failure_code,
                  failure_timestamp, attempt_count, status, customer_opt_out, last_action_timestamp
           FROM transactions WHERE batch_id = ?""",
        (current_batch,),
    )
    cur.execute(
        """INSERT INTO receivables_snapshot
           SELECT invoice_id, customer_id, amount, due_date, days_overdue, contact_history, status,
                  customer_opt_out, attempt_count, last_action_timestamp
           FROM receivables WHERE batch_id = ?""",
        (current_batch,),
    )
    # Explicit column list (not SELECT *): checkout_abandonments has a
    # recovered_amount column (Phase 1 fix) and a batch_id column (Phase 2)
    # that checkout_abandonments_snapshot deliberately does not — the
    # snapshot is taken before anything has been executed, so
    # recovered_amount is always NULL at snapshot time anyway, and a bare
    # SELECT * would break on column-count mismatch.
    cur.execute(
        """INSERT INTO checkout_abandonments_snapshot
           SELECT session_id, customer_id, cart_value, abandoned_at, payment_attempted,
                  customer_opt_out, attempt_count, last_action_timestamp, status
           FROM checkout_abandonments WHERE batch_id = ?""",
        (current_batch,),
    )
    conn.commit()
    conn.close()


if __name__ == "__main__":
    init_db(reset=True)
