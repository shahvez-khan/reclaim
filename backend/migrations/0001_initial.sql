
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
    last_action_timestamp  TEXT                             -- used to enforce 24h cool-off
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
    last_action_timestamp   TEXT
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
    status               TEXT NOT NULL DEFAULT 'open'  -- open / recovering / recovered / lost / escalated / stopped
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
    diagnosed_at    TEXT NOT NULL
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
    ml_selected_action  TEXT            -- the granular candidate action the ML/EV comparison picked (e.g. RETRY_LATER), distinct from the broad `action` execution category
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
    actor           TEXT NOT NULL        -- 'agent' or 'human'
);

-- Pristine copies of transactions/receivables taken immediately after data
-- generation, before either the agent pipeline or the baseline strategy
-- mutates anything. Both are scored against this identical starting point so
-- baseline-vs-agent is a fair, apples-to-apples comparison.
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

