"""
Test for the re-plan loop's termination guarantee (agent_loop.py): every
transaction terminates by recovering, exhausting candidate actions, or
hitting the attempt cap — and NEVER runs past SAFETY_ITERATION_CAP, even in
the worst case where every single automated attempt fails.

This uses a real (temp-file) SQLite DB with the actual schema, a real trained
model (via candidate_actions/decision), and a monkeypatched executor forced
to always fail — so it's exercising the real loop logic, not a mock of it.

Run with pytest, or directly via `python3 -m tests.test_agent_loop_termination`.
"""

import atexit
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import config as _config  # noqa: E402 — must patch DB_PATH before importing schema/agent_loop


def _make_temp_db():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    return Path(tmp.name)


DB_PATH_OVERRIDE = _make_temp_db()
_config.DB_PATH = DB_PATH_OVERRIDE
# Every sibling test file cleans up its temp DB in a try/finally around its
# (single) test function; this file's DB is module-level, shared by two
# test functions, so there's no single function body to wrap. Confirmed
# (see BUG_SWEEP_LOG.md pass 9) that under pytest — as opposed to this
# file's standalone `if __name__ == "__main__"` runner, whose own cleanup
# at the bottom only runs in that path — nothing was ever deleting this
# file, leaking one temp .db per pytest invocation. atexit covers both
# entry points with one line.
atexit.register(lambda: DB_PATH_OVERRIDE.unlink(missing_ok=True))

import schema  # noqa: E402

schema.DB_PATH = DB_PATH_OVERRIDE
schema.init_db(reset=True)

import agent_loop  # noqa: E402
import execution  # noqa: E402
from decision import MAX_ATTEMPTS  # noqa: E402


class AlwaysFailExecutor:
    """Forces every automated action to fail — the worst case for loop
    termination, since it guarantees the loop can never exit via recovery."""

    def retry_payment(self, record_id, amount, failure_code):
        return {"provider": "test_mock", "record_id": record_id, "status": "failed", "amount": amount}

    def send_outreach(self, record_id, channel, action):
        return {"provider": "test_mock", "record_id": record_id, "delivery_status": "delivered",
                "follow_up_payment_received": False}


def _seed_transaction(conn, failure_code="issuer_decline", risk_flag=0):
    """issuer_decline has 3 candidate actions (RETRY_NOW, RETRY_LATER,
    ALTERNATE_PAYMENT_METHOD) — the most iterations any single failure_code
    can produce, making it the right stress case for termination."""
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        """INSERT INTO transactions (transaction_id, customer_id, amount, currency, payment_method,
           failure_code, failure_timestamp, attempt_count, status, customer_opt_out, last_action_timestamp)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        ("txn_loop_test", "cust_loop_test", 1000.0, "INR", "card", failure_code, now, 0, "open", 0, None),
    )
    conn.execute(
        """INSERT INTO diagnoses (record_id, record_type, root_cause, confidence, risk_flag,
           needs_manual_followup, recommended_urgency, diagnosed_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        ("txn_loop_test", "transaction", "test root cause", 0.8, risk_flag, 0, "immediate", now),
    )
    conn.commit()


def test_loop_terminates_and_never_exceeds_safety_cap_when_every_attempt_fails():
    original_executor = execution._executor
    execution._executor = AlwaysFailExecutor()
    try:
        conn = schema.get_connection()
        conn.execute("DELETE FROM transactions")
        conn.execute("DELETE FROM diagnoses")
        conn.execute("DELETE FROM decisions")
        conn.execute("DELETE FROM audit_log")
        conn.commit()
        _seed_transaction(conn, failure_code="issuer_decline")

        row = conn.execute("SELECT * FROM transactions WHERE transaction_id = ?", ("txn_loop_test",)).fetchone()
        diag = conn.execute("SELECT * FROM diagnoses WHERE record_id = ?", ("txn_loop_test",)).fetchone()

        result = agent_loop.run_agentic_transaction(row, diag, conn)
        conn.commit()

        # never exceeds the safety cap
        assert result["attempts"] <= agent_loop.SAFETY_ITERATION_CAP, (
            f"loop ran {result['attempts']} attempts, safety cap is {agent_loop.SAFETY_ITERATION_CAP}"
        )

        # with every attempt failing and only 3 candidate actions for
        # issuer_decline, it should terminate via candidate exhaustion / max
        # attempts well before the safety cap — never recovered, since we forced
        # every attempt to fail
        assert result["final_status"] != "recovered"

        # the last decision recorded must be a terminal one (escalate or stop),
        # not another automated action left hanging
        conn2 = schema.get_connection()
        last_decision = conn2.execute(
            "SELECT * FROM decisions WHERE record_id = ? ORDER BY attempt_number DESC LIMIT 1",
            ("txn_loop_test",),
        ).fetchone()
        assert last_decision["action"] in ("escalate_to_human", "stop_no_action"), (
            f"loop ended without a terminal decision — last action was {last_decision['action']!r}"
        )
        conn2.close()
        conn.close()
    finally:
        execution._executor = original_executor  # isolation: don't leak the always-fail executor to other tests


def test_loop_terminates_even_with_generous_cap_override():
    """Even if SAFETY_ITERATION_CAP were misconfigured very high, the loop
    still can't run forever for a single transaction, because MAX_ATTEMPTS
    and finite candidate actions force a terminal decision well before any
    reasonable cap is reached."""
    original_executor = execution._executor
    execution._executor = AlwaysFailExecutor()
    original_cap = agent_loop.SAFETY_ITERATION_CAP
    agent_loop.SAFETY_ITERATION_CAP = 50
    try:
        conn = schema.get_connection()
        conn.execute("DELETE FROM transactions")
        conn.execute("DELETE FROM diagnoses")
        conn.execute("DELETE FROM decisions")
        conn.execute("DELETE FROM audit_log")
        conn.commit()
        _seed_transaction(conn, failure_code="issuer_decline")

        row = conn.execute("SELECT * FROM transactions WHERE transaction_id = ?", ("txn_loop_test",)).fetchone()
        diag = conn.execute("SELECT * FROM diagnoses WHERE record_id = ?", ("txn_loop_test",)).fetchone()
        result = agent_loop.run_agentic_transaction(row, diag, conn)
        conn.commit()
        conn.close()

        # terminates well before the inflated cap, driven by MAX_ATTEMPTS / candidate exhaustion
        assert result["attempts"] <= MAX_ATTEMPTS + 1, (
            f"expected termination near MAX_ATTEMPTS ({MAX_ATTEMPTS}), got {result['attempts']} attempts "
            f"even with a generous cap of 50"
        )
    finally:
        agent_loop.SAFETY_ITERATION_CAP = original_cap
        execution._executor = original_executor  # isolation: don't leak the always-fail executor to other tests


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
