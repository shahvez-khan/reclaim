"""
Regression test for the discount-recovery accounting bug (Feature
Completion Loop, Phase 1): when an abandoned cart is recovered via
send_discount_nudge, backend/execution.py used to record the FULL
cart_value as recovered revenue, even though a discount nudge by
construction means the customer paid less than cart_value. This overstated
real recovered revenue for every discount-nudge recovery.

BEFORE THE FIX this test would have failed: `checkout_abandonments` had no
`recovered_amount` column at all (this test's first assertion — that the
column exists and is populated — would have raised sqlite3.OperationalError:
no such column), and execution.py's run_execution() summed gross cart_value
for every recovered abandonment regardless of which action recovered it, so
even with the column bolted on separately, a discount-nudge recovery's
recorded amount would equal cart_value instead of cart_value*(1-DISCOUNT_PCT).

This test forces two abandonment records through run_execution() with a
controlled executor that always reports a follow-up payment: one recovered
via send_cart_recovery_link (no discount), one via send_discount_nudge
(discounted). It asserts recovered_amount == cart_value for the former and
recovered_amount == cart_value*(1-DISCOUNT_PCT) (i.e. strictly less than
cart_value) for the latter.

Run with pytest, or directly via `python3 -m tests.test_discount_recovery_accounting`.
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


class AlwaysRecoversExecutor:
    """Every outreach send is reported as leading to a follow-up payment —
    isolates the accounting logic from the mock's random follow-through
    probabilities, which would make this test flaky."""

    def retry_payment(self, record_id, amount, failure_code):
        return {"provider": "test_mock", "record_id": record_id, "status": "failed", "amount": amount}

    def send_outreach(self, record_id, channel, action):
        return {"provider": "test_mock", "record_id": record_id, "delivery_status": "delivered",
                "follow_up_payment_received": True}


def _seed_abandonments(conn, batch_id):
    now = datetime.now().isoformat(timespec="seconds")
    # No discount: plain cart-recovery link, cart value under the discount
    # gate's minimum anyway (see candidate_actions.DISCOUNT_NUDGE_MIN_CART_VALUE).
    conn.execute(
        """INSERT INTO checkout_abandonments (session_id, customer_id, cart_value, abandoned_at,
           payment_attempted, customer_opt_out, attempt_count, last_action_timestamp, status, batch_id)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        ("sess_discount_test_plain", "cust_discount_test", 1500.0, now, 0, 0, 0, None, "open", batch_id),
    )
    # Discount-eligible: cart value above DISCOUNT_NUDGE_MIN_CART_VALUE.
    conn.execute(
        """INSERT INTO checkout_abandonments (session_id, customer_id, cart_value, abandoned_at,
           payment_attempted, customer_opt_out, attempt_count, last_action_timestamp, status, batch_id)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        ("sess_discount_test_discounted", "cust_discount_test", 5000.0, now, 0, 0, 1, None, "open", batch_id),
    )
    for rid in ("sess_discount_test_plain", "sess_discount_test_discounted"):
        conn.execute(
            """INSERT INTO diagnoses (record_id, record_type, root_cause, confidence, risk_flag,
               needs_manual_followup, recommended_urgency, diagnosed_at, batch_id)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (rid, "abandonment", "test root cause", 0.8, 0, 0, "within_24h", now, batch_id),
        )
    # Decisions inserted directly (bypassing candidate scoring — this test is
    # about execution-time accounting, not candidate selection).
    conn.execute(
        """INSERT INTO decisions (decision_id, record_id, record_type, attempt_number, action,
           reasoning, decided_at, batch_id) VALUES (?,?,?,?,?,?,?,?)""",
        ("dec_discount_test_plain", "sess_discount_test_plain", "abandonment", 1,
         "send_cart_recovery_link", "test reasoning", now, batch_id),
    )
    conn.execute(
        """INSERT INTO decisions (decision_id, record_id, record_type, attempt_number, action,
           reasoning, decided_at, batch_id) VALUES (?,?,?,?,?,?,?,?)""",
        ("dec_discount_test_discounted", "sess_discount_test_discounted", "abandonment", 1,
         "send_discount_nudge", "test reasoning", now, batch_id),
    )
    conn.commit()


def test_discount_nudge_recovery_is_net_of_discount():
    """recovered_amount must be strictly less than cart_value (by exactly
    DISCOUNT_PCT) for a discount-nudge recovery, and exactly equal to
    cart_value for a non-discount recovery."""
    db_path = _make_temp_db()
    original_config_db_path = _config.DB_PATH
    _config.DB_PATH = db_path

    import schema
    original_db_path = schema.DB_PATH
    try:
        schema.DB_PATH = db_path
        schema.init_db(reset=True)

        import execution
        from candidate_actions import DISCOUNT_PCT

        original_executor = execution._executor
        execution._executor = AlwaysRecoversExecutor()
        try:
            conn = schema.get_connection()
            batch_id = schema.create_batch(conn, {"transactions": 0, "receivables": 0, "checkout_abandonments": 2})
            conn.commit()
            _seed_abandonments(conn, batch_id)

            execution.run_execution(types=("abandonment",))

            plain = conn.execute(
                "SELECT cart_value, recovered_amount, status FROM checkout_abandonments WHERE session_id = ?",
                ("sess_discount_test_plain",),
            ).fetchone()
            discounted = conn.execute(
                "SELECT cart_value, recovered_amount, status FROM checkout_abandonments WHERE session_id = ?",
                ("sess_discount_test_discounted",),
            ).fetchone()
            conn.close()

            assert plain["status"] == "recovered"
            assert discounted["status"] == "recovered"

            # Non-discount recovery: net == gross.
            assert plain["recovered_amount"] == plain["cart_value"], (
                f"non-discount recovery should recover the full cart_value, got "
                f"recovered_amount={plain['recovered_amount']} vs cart_value={plain['cart_value']}"
            )

            # Discount-nudge recovery: net strictly less than gross, by exactly DISCOUNT_PCT.
            assert discounted["recovered_amount"] < discounted["cart_value"], (
                "discount-nudge recovery must recover LESS than the full cart_value — "
                f"got recovered_amount={discounted['recovered_amount']} >= "
                f"cart_value={discounted['cart_value']}"
            )
            expected = discounted["cart_value"] * (1 - DISCOUNT_PCT)
            assert abs(discounted["recovered_amount"] - expected) < 0.01, (
                f"expected recovered_amount={expected:.4f} (cart_value * (1 - {DISCOUNT_PCT})), "
                f"got {discounted['recovered_amount']}"
            )

            print(f"plain:      cart_value={plain['cart_value']}, recovered_amount={plain['recovered_amount']} (net == gross, correct)")
            print(f"discounted: cart_value={discounted['cart_value']}, recovered_amount={discounted['recovered_amount']} "
                  f"(net of {DISCOUNT_PCT*100:.0f}% discount, correct)")
        finally:
            execution._executor = original_executor
    finally:
        schema.DB_PATH = original_db_path
        _config.DB_PATH = original_config_db_path
        db_path.unlink(missing_ok=True)


if __name__ == "__main__":
    import traceback
    try:
        test_discount_nudge_recovery_is_net_of_discount()
        print("\nPASSED")
    except Exception:
        traceback.print_exc()
        print("\nFAILED")
        sys.exit(1)
