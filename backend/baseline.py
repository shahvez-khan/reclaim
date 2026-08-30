"""
Phase D: baseline vs. AI agent comparison.

BASELINE STRATEGY (intentionally dumb, per the spec):
  Transactions:  always RETRY_NOW, blindly, regardless of failure_code.
  Receivables:   always SEND_REMINDER once, regardless of days_overdue.
  Abandonments:  always SEND_CART_RECOVERY_LINK once, regardless of recency
                 or whether payment was ever attempted — the discount-nudge
                 policy gate never applies to the baseline since it never
                 even considers a second candidate action.

The baseline is NOT diagnosis-aware, NOT ML-scored, and NEVER re-plans after
a failure — it tries exactly once and stops. It DOES sit behind the exact
same hard policy floor as the agent (opt-out, risk_flag, max-attempts,
cool-off — via decision.hard_policy_gate, shared code, not a re-implementation)
because a fair baseline has to obey the same compliance constraints any real
system would; otherwise "baseline vs agent" would really be measuring
"non-compliant vs compliant" rather than "naive vs smart action selection",
which is not the comparison this is supposed to make. Everything else the
agent does (candidate comparison, expected value, smart retry timing,
re-planning) is absent here by design, so the delta isolates the value the
AI/agentic layer adds ON TOP OF an identical compliance floor.

Runs against the PRISTINE SNAPSHOT (schema.snapshot_initial_data()) so it is
scored against the exact same starting point as the agent pipeline, never
against agent-mutated state.
"""

import random

from schema import get_connection
from decision import hard_policy_gate

random.seed(999)  # independent of data-gen (42) and execution (123) seeds

# Same base probabilities as execution.py's mock Razorpay/outreach layer —
# the baseline uses the identical simulator, just picks a dumber action.
RETRY_SUCCESS_PROB = {
    "bank_timeout": 0.65, "issuer_decline": 0.45, "insufficient_funds": 0.40,
    "otp_failed": 0.55, "expired_card": 0.0, "risk_block": 0.0,
}
SEND_REMINDER_PROB = 0.25
CART_RECOVERY_LINK_PROB = 0.22  # same base rate as execution.py's mock for this action


def run_baseline():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM baseline_results")

    diag_by_id = {d["record_id"]: d for d in cur.execute("SELECT * FROM diagnoses").fetchall()}
    results = []

    for row in cur.execute("SELECT * FROM transactions_snapshot").fetchall():
        amount = row["amount"]
        diag = diag_by_id[row["transaction_id"]]
        gate = hard_policy_gate(row, diag, "transaction")
        if gate:
            # Same compliance floor as the agent — a blocked record earns the
            # baseline zero recovery too, exactly as it would the agent.
            action, outcome, recovered = gate["action"], f"Blocked by policy: {gate['note']}", 0
        else:
            p = RETRY_SUCCESS_PROB.get(row["failure_code"], 0.3)
            success = random.random() < p
            action = "retry_payment"
            outcome = f"Blind retry succeeded — ₹{amount:,.2f} recovered." if success else "Blind retry failed."
            recovered = int(success)
        results.append(("transaction", row["transaction_id"], amount, action, outcome, recovered))

    for row in cur.execute("SELECT * FROM receivables_snapshot").fetchall():
        amount = row["amount"]
        diag = diag_by_id[row["invoice_id"]]
        gate = hard_policy_gate(row, diag, "receivable")
        if gate:
            action, outcome, recovered = gate["action"], f"Blocked by policy: {gate['note']}", 0
        else:
            success = random.random() < SEND_REMINDER_PROB
            action = "send_reminder"
            outcome = f"Reminder led to payment — ₹{amount:,.2f} recovered." if success else "Reminder sent, no payment yet."
            recovered = int(success)
        results.append(("receivable", row["invoice_id"], amount, action, outcome, recovered))

    for row in cur.execute("SELECT * FROM checkout_abandonments_snapshot").fetchall():
        amount = row["cart_value"]
        diag = diag_by_id[row["session_id"]]
        gate = hard_policy_gate(row, diag, "abandonment")
        if gate:
            action, outcome, recovered = gate["action"], f"Blocked by policy: {gate['note']}", 0
        else:
            success = random.random() < CART_RECOVERY_LINK_PROB
            action = "send_cart_recovery_link"
            outcome = f"Cart recovery link led to checkout — ₹{amount:,.2f} recovered." if success else "Cart recovery link sent, no checkout yet."
            recovered = int(success)
        # Phase 1 discount-accounting fix: net-of-discount accounting only
        # matters when a discount action is actually used. VERIFIED (not
        # assumed) here — this loop's `action` is hard-coded to
        # "send_cart_recovery_link" above; SEND_DISCOUNT_NUDGE is never
        # reachable from the baseline strategy (it doesn't consult
        # candidate_actions.py at all, so the discount policy gate never
        # even runs). `amount` (== cart_value) is therefore already the true
        # recovered figure for every baseline abandonment row — no separate
        # recovered_amount accounting needed on this path.
        assert action != "send_discount_nudge"
        results.append(("abandonment", row["session_id"], amount, action, outcome, recovered))

    cur.executemany(
        "INSERT INTO baseline_results (record_id, record_type, amount, baseline_action, baseline_outcome, baseline_recovered) VALUES (?,?,?,?,?,?)",
        [(rid, rtype, amount, action, outcome, recovered) for rtype, rid, amount, action, outcome, recovered in results],
    )
    conn.commit()

    total_amount = sum(r[2] for r in results)
    recovered_amount = sum(r[2] for r in results if r[5])
    print(f"Baseline: {len(results)} records, ₹{recovered_amount:,.2f} recovered of ₹{total_amount:,.2f} ({recovered_amount/total_amount*100:.1f}%)")

    conn.close()
    return {"total_amount": total_amount, "recovered_amount": recovered_amount, "n_records": len(results)}


if __name__ == "__main__":
    run_baseline()
