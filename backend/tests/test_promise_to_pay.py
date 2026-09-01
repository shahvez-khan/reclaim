"""
Unit tests for the promise-to-pay tracker (Feature Completion Loop, Phase
3): turns the "promise-to-pay outreach" LABEL that already existed in
diagnosis.py's 45+ day overdue tier text into a real, tracked feature with
promised_pay_date/promise_status fields that measurably change candidate
eligibility and the decision itself — not just diagnosis wording.

Covers:
  - candidates_for_receivable: a BROKEN promise makes ESCALATE_REMINDER
    eligible immediately even for a days_overdue < 15 invoice that would
    otherwise only be eligible for SEND_REMINDER (the exact case named in
    the phase spec's Verify block).
  - decide_receivable: a PENDING promise is an explicit policy-level HOLD
    (stop_no_action / stopping_rule_fired == 'promise_pending'), checked
    before any candidate is even generated — mirrors the project's existing
    24h-cooldown pattern.

Run with pytest, or directly via `python3 -m tests.test_promise_to_pay`.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from candidate_actions import candidates_for_receivable
from decision import decide_receivable


def make_receivable_row(**overrides):
    row = {
        "invoice_id": "inv_test1",
        "customer_id": "cust_test1",
        "amount": 50000.0,
        "days_overdue": 10,
        "customer_opt_out": 0,
        "attempt_count": 0,
        "last_action_timestamp": None,
        "promise_status": "none",
        "promised_pay_date": None,
    }
    row.update(overrides)
    return row


def make_diag_row(**overrides):
    row = {"root_cause": "test root cause", "risk_flag": 0}
    row.update(overrides)
    return row


# --- candidates_for_receivable ---

def test_broken_promise_makes_escalate_reminder_eligible_even_under_15_days():
    """The exact case named in the Phase 3 Verify block: a broken-promise
    receivable with days_overdue < 15 (which would normally ONLY be eligible
    for SEND_REMINDER) must still include ESCALATE_REMINDER once
    promise_status == 'broken'."""
    candidates = candidates_for_receivable(days_overdue=10, promise_status="broken")
    assert "ESCALATE_REMINDER" in candidates, (
        f"a broken promise must make ESCALATE_REMINDER eligible regardless of days_overdue tier, got {candidates}"
    )
    assert "SEND_REMINDER" in candidates


def test_no_promise_under_15_days_only_gets_send_reminder():
    """Sanity baseline: WITHOUT a broken promise, a fresh (< 15 day) invoice
    is only eligible for the soft SEND_REMINDER tier — confirms the broken-
    promise override above is actually doing something, not just always-on."""
    candidates = candidates_for_receivable(days_overdue=10, promise_status="none")
    assert candidates == ["SEND_REMINDER"]
    assert "ESCALATE_REMINDER" not in candidates


def test_pending_promise_does_not_change_candidate_eligibility():
    """A pending promise is handled entirely upstream as a policy-level HOLD
    (see decide_receivable test below) — candidates_for_receivable itself
    should never even be reached for a pending-promise record in the real
    pipeline, but if called directly it falls back to plain day-count
    tiering (pending isn't a broken-promise override)."""
    candidates = candidates_for_receivable(days_overdue=10, promise_status="pending")
    assert candidates == ["SEND_REMINDER"]


def test_broken_promise_at_45_plus_days_still_includes_escalate_reminder():
    """Non-override case: a 45+ day overdue invoice already gets
    ESCALATE_REMINDER by day count alone — a broken promise on top of that
    must not somehow REMOVE it."""
    candidates = candidates_for_receivable(days_overdue=60, promise_status="broken")
    assert "ESCALATE_REMINDER" in candidates


# --- decide_receivable: pending promise is a policy-level hold ---

def test_pending_promise_holds_regardless_of_days_overdue():
    """A pending promise must produce stop_no_action with
    stopping_rule_fired == 'promise_pending' — checked BEFORE any candidate
    is generated, so it can never be out-ranked by expected value."""
    row = make_receivable_row(days_overdue=60, promise_status="pending", promised_pay_date="2099-01-01")
    diag = make_diag_row()
    decision = decide_receivable(row, diag)
    assert decision["action"] == "stop_no_action"
    assert decision["stopping_rule_fired"] == "promise_pending"
    assert "promise" in decision["reasoning"].lower()


def test_broken_promise_does_not_trigger_the_pending_hold():
    """A broken promise (the opposite state) must NOT be held — it should
    proceed to normal ML-scored decision-making with ESCALATE_REMINDER
    eligible, not get stuck in the pending-hold branch."""
    row = make_receivable_row(days_overdue=10, promise_status="broken", promised_pay_date="2020-01-01")
    diag = make_diag_row()
    decision = decide_receivable(row, diag)
    assert decision["action"] != "stop_no_action"
    assert decision["stopping_rule_fired"] != "promise_pending"


def test_no_promise_is_unaffected_by_promise_logic():
    """Baseline: a record with promise_status='none' must behave exactly as
    it did before this phase — normal ML-scored decision, no promise-related
    stopping rule."""
    row = make_receivable_row(days_overdue=10, promise_status="none")
    diag = make_diag_row()
    decision = decide_receivable(row, diag)
    assert decision["stopping_rule_fired"] != "promise_pending"


if __name__ == "__main__":
    import traceback
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASSED: {t.__name__}")
        except Exception:
            failed += 1
            print(f"FAILED: {t.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    if failed:
        sys.exit(1)
