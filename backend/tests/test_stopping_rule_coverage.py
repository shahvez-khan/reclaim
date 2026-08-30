"""
Regression test for the "unaccounted-for escalations" bug (UI fixes loop,
Phase 1): decision.decide_transaction had a branch where a transaction runs
out of untried candidate actions and escalates to a human, but the
stopping_rule was passed as None instead of a real value. This silently
excluded those escalations from the dashboard's "Stopping Rules Fired"
panel — 24 out of 221 escalations (~11%) in one real run — while still
counting them in the "Human escalations" KPI, so the two numbers didn't
reconcile and the panel's "every rule below blocked an automated action"
claim was false for that fraction of records.

BEFORE THE FIX this test would have failed: the "no candidates remain"
branch in decide_transaction returned `_decision(..., None, ...)` for its
stopping_rule argument whenever a transaction (a) had a failure_code with
only one candidate action to begin with (e.g. otp_failed -> RETRY_NOW only)
or (b) exhausted every candidate across re-plan attempts before hitting
MAX_ATTEMPTS. This test forces exactly case (a) — a fixed-seed otp_failed
transaction with attempt_count already at MAX_ATTEMPTS-1, one candidate,
guaranteed to fail (AlwaysFailExecutor) — and asserts a real
stopping_rule_fired value comes back, not None.

Run with pytest, or directly via `python3 -m tests.test_stopping_rule_coverage`.
"""

import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import config as _config  # noqa: E402


def _make_temp_db():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    return Path(tmp.name)


DB_PATH_OVERRIDE = _make_temp_db()
_config.DB_PATH = DB_PATH_OVERRIDE

import schema  # noqa: E402
schema.DB_PATH = DB_PATH_OVERRIDE
schema.init_db(reset=True)

import agent_loop  # noqa: E402
import execution  # noqa: E402


class AlwaysFailExecutor:
    def retry_payment(self, record_id, amount, failure_code):
        return {"provider": "test_mock", "record_id": record_id, "status": "failed", "amount": amount}

    def send_outreach(self, record_id, channel, action):
        return {"provider": "test_mock", "record_id": record_id, "delivery_status": "delivered",
                "follow_up_payment_received": False}


def _seed_otp_failed_transaction(conn):
    """otp_failed has exactly ONE candidate action (RETRY_NOW) — the single-
    candidate case that triggers the candidates_exhausted branch on its very
    first automated attempt, well before MAX_ATTEMPTS is reached."""
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        """INSERT INTO transactions (transaction_id, customer_id, amount, currency, payment_method,
           failure_code, failure_timestamp, attempt_count, status, customer_opt_out, last_action_timestamp)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        ("txn_stoprule_test", "cust_stoprule_test", 1000.0, "INR", "card", "otp_failed", now, 0, "open", 0, None),
    )
    conn.execute(
        """INSERT INTO diagnoses (record_id, record_type, root_cause, confidence, risk_flag,
           needs_manual_followup, recommended_urgency, diagnosed_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        ("txn_stoprule_test", "transaction", "test root cause", 0.8, 0, 0, "immediate", now),
    )
    conn.commit()


def test_no_escalation_has_null_stopping_rule():
    """Every latest decision with action == escalate_to_human must have a
    real (non-null) stopping_rule_fired — whether it came from
    hard_policy_gate (risk_flag/max_attempts/etc.) or from candidate
    exhaustion inside decide_transaction (candidates_exhausted)."""
    original_executor = execution._executor
    execution._executor = AlwaysFailExecutor()
    try:
        conn = schema.get_connection()
        conn.execute("DELETE FROM transactions")
        conn.execute("DELETE FROM diagnoses")
        conn.execute("DELETE FROM decisions")
        conn.execute("DELETE FROM audit_log")
        conn.commit()
        _seed_otp_failed_transaction(conn)

        row = conn.execute("SELECT * FROM transactions WHERE transaction_id = ?", ("txn_stoprule_test",)).fetchone()
        diag = conn.execute("SELECT * FROM diagnoses WHERE record_id = ?", ("txn_stoprule_test",)).fetchone()
        result = agent_loop.run_agentic_transaction(row, diag, conn)
        conn.commit()

        assert result["final_status"] != "recovered"  # forced to always fail

        last_decision = conn.execute(
            "SELECT * FROM decisions WHERE record_id = ? ORDER BY attempt_number DESC LIMIT 1",
            ("txn_stoprule_test",),
        ).fetchone()
        assert last_decision["action"] == "escalate_to_human", (
            f"expected the single-candidate otp_failed transaction to escalate once RETRY_NOW "
            f"fails, got action={last_decision['action']!r}"
        )
        assert last_decision["stopping_rule_fired"] is not None, (
            "REGRESSION: an escalate_to_human decision has stopping_rule_fired=None — this is "
            "exactly the bug where candidate-exhaustion escalations were invisible to the "
            "'Stopping Rules Fired' panel. Expected 'candidates_exhausted'."
        )
        assert last_decision["stopping_rule_fired"] == "candidates_exhausted", (
            f"expected 'candidates_exhausted' (only one candidate existed for otp_failed), "
            f"got {last_decision['stopping_rule_fired']!r}"
        )
        conn.close()
    finally:
        execution._executor = original_executor


def test_bucket_and_breakdown_reconcile_on_full_dataset():
    """The dashboard-level invariant this bug broke: every escalated record
    (bucket_counts['escalated']) must be explained by SOME row in the
    stopping_rule_breakdown — summing risk_flag + max_attempts +
    candidates_exhausted (the only three rules that ever produce
    action == escalate_to_human) must exactly equal the escalated count.
    Runs against whatever the real project DB currently has — this test is
    meant to be run after a real pipeline run, not in isolation like the
    test above.

    ISOLATION WARNING this test has to respect: schema.DB_PATH is a shared
    module-level global. Pointing it at the real project DB and forgetting
    to restore it afterward corrupts every OTHER test that runs later in
    the same process — an earlier version of this test did exactly that:
    it left schema.DB_PATH pointed at the real DB, and the next test's
    `DELETE FROM transactions` (meant for its own isolated temp DB) wiped
    real project data instead. Fixed with an explicit try/finally restore
    below — never remove it.
    """
    import config
    real_db_path = config.PROJECT_ROOT / "data" / "revenue_recovery.db"
    if not real_db_path.exists():
        return  # nothing to check yet — fine to no-op before the first `run_pipeline.py`

    original_db_path = schema.DB_PATH
    try:
        schema.DB_PATH = real_db_path  # temporarily point at the real project DB for this READ-ONLY check
        conn = schema.get_connection()
        cur = conn.cursor()

        decisions = cur.execute("SELECT * FROM decisions ORDER BY attempt_number").fetchall()
        latest = {}
        for d in decisions:
            latest[d["record_id"]] = d

        escalated = [d for d in latest.values() if d["action"] == "escalate_to_human"]
        escalation_rules = {"risk_flag", "max_attempts", "candidates_exhausted"}
        accounted_for = [d for d in escalated if d["stopping_rule_fired"] in escalation_rules]

        conn.close()

        assert len(accounted_for) == len(escalated), (
            f"{len(escalated) - len(accounted_for)} escalated record(s) have a stopping_rule_fired "
            f"value outside {escalation_rules} (or None) — the compliance panel would under-report."
        )
    finally:
        schema.DB_PATH = original_db_path  # CRITICAL: never leave schema.DB_PATH pointed at the real DB


if __name__ == "__main__":
    import traceback
    tests = [(name, fn) for name, fn in list(globals().items()) if name.startswith("test_") and callable(fn)]
    passed, failed = 0, 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL  {name}: {e}")
            failed += 1
        except Exception:
            print(f"  ERROR {name}:")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    DB_PATH_OVERRIDE.unlink(missing_ok=True)
    sys.exit(1 if failed else 0)
