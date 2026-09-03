"""
Unit tests for decision.hard_policy_gate — per the README, THIS function IS
the compliance guarantee, so it gets the heaviest coverage in the project:
opt-out always wins, risk-flag always escalates, max-attempts always
escalates, cool-off blocks correctly, and — critically — policy order can't
be bypassed by any combination of inputs.

Run with pytest (test discovery finds every `test_*` function below), or
directly via `python3 -m tests.test_policy_gate` for a quick manual check
without pytest installed (see the __main__ block).
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from decision import COOLDOWN_HOURS, MAX_ATTEMPTS, hard_policy_gate


def make_row(**overrides):
    row = {
        "transaction_id": "txn_test1",
        "customer_opt_out": 0,
        "attempt_count": 0,
        "last_action_timestamp": None,
    }
    row.update(overrides)
    return row


def make_diag(risk_flag=0):
    return {"risk_flag": risk_flag}


def recent_timestamp(hours_ago: float) -> str:
    return (datetime.now() - timedelta(hours=hours_ago)).isoformat(timespec="seconds")


# --- Rule 1: opt-out always wins, no exceptions ---

def test_opt_out_blocks():
    row = make_row(customer_opt_out=1)
    result = hard_policy_gate(row, make_diag(), "transaction")
    assert result is not None
    assert result["action"] == "stop_no_action"
    assert result["stopping_rule"] == "customer_opt_out"


def test_opt_out_wins_even_with_risk_flag():
    """opt_out must be checked BEFORE risk_flag — if a customer opted out,
    that holds even for a record that's also flagged as risky."""
    row = make_row(customer_opt_out=1)
    result = hard_policy_gate(row, make_diag(risk_flag=1), "transaction")
    assert result["stopping_rule"] == "customer_opt_out"
    assert result["action"] == "stop_no_action"  # NOT escalate_to_human


def test_opt_out_wins_even_with_max_attempts():
    row = make_row(customer_opt_out=1, attempt_count=99)
    result = hard_policy_gate(row, make_diag(), "transaction")
    assert result["stopping_rule"] == "customer_opt_out"


def test_opt_out_wins_even_during_cooldown():
    row = make_row(customer_opt_out=1, last_action_timestamp=recent_timestamp(1))
    result = hard_policy_gate(row, make_diag(), "transaction")
    assert result["stopping_rule"] == "customer_opt_out"


# --- Rule 2: risk_flag always escalates (never auto-retried) ---

def test_risk_flag_escalates():
    row = make_row()
    result = hard_policy_gate(row, make_diag(risk_flag=1), "transaction")
    assert result is not None
    assert result["action"] == "escalate_to_human"
    assert result["stopping_rule"] == "risk_flag"


def test_risk_flag_wins_over_max_attempts_and_cooldown():
    row = make_row(attempt_count=99, last_action_timestamp=recent_timestamp(1))
    result = hard_policy_gate(row, make_diag(risk_flag=1), "transaction")
    assert result["stopping_rule"] == "risk_flag"
    assert result["action"] == "escalate_to_human"


# --- Rule 3: max_attempts always escalates ---

def test_max_attempts_escalates():
    row = make_row(attempt_count=MAX_ATTEMPTS)
    result = hard_policy_gate(row, make_diag(), "transaction")
    assert result is not None
    assert result["action"] == "escalate_to_human"
    assert result["stopping_rule"] == "max_attempts"


def test_below_max_attempts_does_not_escalate_on_this_rule():
    row = make_row(attempt_count=MAX_ATTEMPTS - 1)
    result = hard_policy_gate(row, make_diag(), "transaction")
    assert result is None  # clear to proceed (no gate fired at all)


def test_max_attempts_wins_over_cooldown():
    row = make_row(attempt_count=MAX_ATTEMPTS, last_action_timestamp=recent_timestamp(1))
    result = hard_policy_gate(row, make_diag(), "transaction")
    assert result["stopping_rule"] == "max_attempts"


# --- Rule 4: cool-off blocks correctly ---

def test_cooldown_blocks_within_window():
    row = make_row(last_action_timestamp=recent_timestamp(COOLDOWN_HOURS - 1))
    result = hard_policy_gate(row, make_diag(), "transaction")
    assert result is not None
    assert result["action"] == "stop_no_action"
    assert result["stopping_rule"] == "cooldown_24h"


def test_cooldown_does_not_block_after_window():
    row = make_row(last_action_timestamp=recent_timestamp(COOLDOWN_HOURS + 1))
    result = hard_policy_gate(row, make_diag(), "transaction")
    assert result is None


def test_no_last_action_timestamp_no_cooldown():
    row = make_row(last_action_timestamp=None)
    result = hard_policy_gate(row, make_diag(), "transaction")
    assert result is None


# --- Clear record: no gate fires, action is permitted ---

def test_clear_record_returns_none():
    row = make_row()
    result = hard_policy_gate(row, make_diag(), "transaction")
    assert result is None


# --- Cross-record-type: same gate logic applies to receivables and abandonments ---

def test_gate_applies_identically_to_receivables():
    row = {"invoice_id": "inv_test1", "customer_opt_out": 1, "attempt_count": 0, "last_action_timestamp": None}
    result = hard_policy_gate(row, make_diag(), "receivable")
    assert result["stopping_rule"] == "customer_opt_out"


def test_gate_applies_identically_to_abandonments():
    row = {"session_id": "sess_test1", "customer_opt_out": 0, "attempt_count": 0, "last_action_timestamp": None}
    result = hard_policy_gate(row, make_diag(risk_flag=1), "abandonment")
    assert result["stopping_rule"] == "risk_flag"


# --- Exhaustive combination test: no combination of inputs can bypass the order ---

def test_priority_order_holds_across_all_combinations():
    """The core compliance property: for EVERY combination of
    (opt_out, risk_flag, attempt_count, cooldown_active), the FIRST
    applicable rule in priority order is the one that fires — never a lower
    one, never none-when-one-should-fire."""
    for opt_out in (0, 1):
        for risk_flag in (0, 1):
            for attempt_count in (0, MAX_ATTEMPTS - 1, MAX_ATTEMPTS, MAX_ATTEMPTS + 5):
                for cooldown in (None, recent_timestamp(1), recent_timestamp(COOLDOWN_HOURS + 1)):
                    row = make_row(customer_opt_out=opt_out, attempt_count=attempt_count, last_action_timestamp=cooldown)
                    diag = make_diag(risk_flag=risk_flag)
                    result = hard_policy_gate(row, diag, "transaction")

                    if opt_out:
                        expected_rule = "customer_opt_out"
                    elif risk_flag:
                        expected_rule = "risk_flag"
                    elif attempt_count >= MAX_ATTEMPTS:
                        expected_rule = "max_attempts"
                    elif cooldown is not None and (datetime.now() - datetime.fromisoformat(cooldown)) < timedelta(hours=COOLDOWN_HOURS):
                        expected_rule = "cooldown_24h"
                    else:
                        expected_rule = None

                    if expected_rule is None:
                        assert result is None, f"expected no gate to fire for {row}, got {result}"
                    else:
                        assert result is not None, f"expected {expected_rule} to fire for {row}, got None"
                        assert result["stopping_rule"] == expected_rule, (
                            f"for opt_out={opt_out}, risk_flag={risk_flag}, attempt_count={attempt_count}, "
                            f"cooldown={cooldown!r}: expected {expected_rule}, got {result['stopping_rule']}"
                        )


if __name__ == "__main__":
    # Manual runner for environments without pytest installed — CI (Phase 4.7)
    # runs this file via pytest for real, but this lets it be verified locally.
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
    sys.exit(1 if failed else 0)
