"""
Loop 4: Execution layer + audit trail.

Every action from the decision stage gets "executed" via a mocked integration
that returns a realistic outcome, then a full audit log entry is written
(record_id, timestamp, action_taken, reasoning, outcome, actor).

Outcome probabilities by failure_code for retry_payment (mocked Razorpay call):
  bank_timeout        -> 65% success (transient, usually resolves on retry)
  issuer_decline       -> 45% success (mixed bag — some are hard blocks)
  insufficient_funds    -> 40% success (only meaningfully improves after a payday-timed delay)
  otp_failed             -> 55% success (usually a UX slip, retries with clearer prompt often work)
  expired_card             -> 0% (should never reach retry_payment; card MUST be updated first)

Outcome probabilities for outreach actions (mocked SMS/email/WhatsApp send +
simulated follow-up "payment received" event):
  send_update_link     -> 55% follow-through (customer updates card & pays)
  send_reminder        -> 25% follow-through (soft nudge, early in overdue window)
  escalate_reminder     -> 35% follow-through (firmer language, more overdue)

escalate_to_human -> no auto outcome, logs a ticket (outcome = pending_human_review)
stop_no_action    -> logs and closes (outcome = closed_no_action)

Outcome probabilities for checkout-abandonment outreach (mocked SMS/email +
simulated follow-up "checkout completed" event):
  send_cart_recovery_link -> 22% follow-through (plain nudge back to cart)
  send_discount_nudge     -> 32% follow-through (higher, but only ever offered
                              per the policy gate in candidate_actions.py, so
                              it's not inflating the blended abandonment number
                              by being used on every record)

MOCK/REAL EXECUTION BOUNDARY (Phase 4.6 of the hardening loop):
All of the above is MockExecutor — a probability table, not a real network
call to Razorpay or any SMS/email/WhatsApp provider. The PaymentExecutor
protocol below is the explicit seam where a real integration would go;
RazorpayExecutor is a documented, deliberately unimplemented stub showing
exactly what that would need (API key, webhook signature verification for
async callbacks). See RazorpayExecutor's docstring for why a real
integration is a bigger refactor than swapping the class in (real gateway
retries are async/webhook-driven, not synchronous like the mock). This
project makes no claim, implicit or explicit, of real Razorpay connectivity
— see README's Production Readiness section.
"""

import random
from datetime import datetime
from typing import Protocol

from schema import get_connection
from config import RAZORPAY_API_KEY, RAZORPAY_WEBHOOK_SECRET, PAYMENT_EXECUTOR
from candidate_actions import DISCOUNT_PCT

random.seed(123)  # reproducible demo outcomes, independent from data-gen seed


class PaymentExecutor(Protocol):
    """The seam a real payment integration would slot into (Phase 4.6). Two
    calls: retry a failed payment, and send an outreach message (SMS/email/
    WhatsApp) with a follow-up-payment observation. MockExecutor below is the
    only implementation actually used today; RazorpayExecutor is a stub that
    shows exactly where real SDK calls would go, deliberately NOT implemented.
    """

    def retry_payment(self, record_id: str, amount: float, failure_code: str | None) -> dict: ...
    def send_outreach(self, record_id: str, channel: str, action: str) -> dict: ...


class MockExecutor:
    """The only executor actually wired up today. Every 'call' below is a
    coin-flip against a hand-set probability table (see module docstring),
    not a real network call — this is a hackathon/demo simulator, not a
    production payment integration. See DATA_SOURCES.md."""

    RETRY_SUCCESS_PROB = {
        "bank_timeout": 0.65,
        "issuer_decline": 0.45,
        "insufficient_funds": 0.40,
        "otp_failed": 0.55,
        "expired_card": 0.0,
        "risk_block": 0.0,
    }

    OUTREACH_FOLLOWTHROUGH_PROB = {
        "send_update_link": 0.55,
        "send_reminder": 0.25,
        "escalate_reminder": 0.35,
        "send_cart_recovery_link": 0.22,
        "send_discount_nudge": 0.32,
    }

    def retry_payment(self, record_id: str, amount: float, failure_code: str | None) -> dict:
        prob = self.RETRY_SUCCESS_PROB.get(failure_code, 0.3)
        success = random.random() < prob
        return {
            "provider": "razorpay_mock", "endpoint": "/v1/payments/retry",
            "record_id": record_id, "status": "success" if success else "failed", "amount": amount,
        }

    def send_outreach(self, record_id: str, channel: str, action: str) -> dict:
        followthrough_prob = self.OUTREACH_FOLLOWTHROUGH_PROB.get(action, 0.2)
        paid = random.random() < followthrough_prob
        return {
            "provider": f"{channel}_mock", "record_id": record_id,
            "delivery_status": "delivered", "follow_up_payment_received": paid,
        }


class RazorpayExecutor:
    """STUB — NOT IMPLEMENTED. Shows exactly where a real Razorpay
    integration would go and what it would need, so this integration point
    is a defined seam rather than hand-waved. Do not use in production
    without implementing the methods below for real.

    What a real implementation would need:
      - RAZORPAY_API_KEY (config.py / RAZORPAY_API_KEY env var) for
        authenticating retry_payment calls against Razorpay's real
        /v1/payments/{id}/capture or a saved-instrument retry API.
      - RAZORPAY_WEBHOOK_SECRET for verifying the HMAC signature on inbound
        async webhook callbacks (payment.captured / payment.failed) — retries
        against a real gateway are NOT synchronous the way MockExecutor's are;
        the real outcome would arrive later via webhook, not as a return
        value from retry_payment() itself. That changes the calling code's
        shape (fire-and-poll/webhook-driven rather than call-and-get-result)
        — a real integration is a bigger refactor than just swapping this
        class in, and that refactor is explicitly out of scope here.
      - A real outreach provider (Twilio/SendGrid/WhatsApp Business API, not
        Razorpay) for send_outreach — Razorpay only handles payment retry,
        not customer messaging.
    """

    def __init__(self, api_key: str = RAZORPAY_API_KEY, webhook_secret: str = RAZORPAY_WEBHOOK_SECRET):
        self.api_key = api_key
        self.webhook_secret = webhook_secret

    def retry_payment(self, record_id: str, amount: float, failure_code: str | None) -> dict:
        raise NotImplementedError(
            "RazorpayExecutor.retry_payment is a defined seam, not a real integration. "
            "Implement a real Razorpay SDK call here before using PAYMENT_EXECUTOR=razorpay."
        )

    def send_outreach(self, record_id: str, channel: str, action: str) -> dict:
        raise NotImplementedError(
            "RazorpayExecutor.send_outreach is a defined seam, not a real integration. "
            "Razorpay itself doesn't send customer messages — wire a real SMS/email/WhatsApp "
            "provider here before using PAYMENT_EXECUTOR=razorpay."
        )


def get_executor() -> PaymentExecutor:
    if PAYMENT_EXECUTOR == "razorpay":
        return RazorpayExecutor()
    return MockExecutor()


_executor = get_executor()


def mock_razorpay_retry(record_id: str, amount: float, failure_code: str) -> dict:
    """Simulates calling the Razorpay retry API. Returns a realistic fake response."""
    return _executor.retry_payment(record_id, amount, failure_code)


def mock_outreach_send(record_id: str, channel: str, action: str) -> dict:
    """Simulates sending SMS/email/WhatsApp — always 'delivered' at the send layer;
    the interesting outcome is whether it leads to a follow-up payment."""
    return _executor.send_outreach(record_id, channel, action)


AUTOMATED_OUTCOME_ACTIONS = {
    "retry_payment", "send_update_link", "send_reminder", "escalate_reminder",
    "send_cart_recovery_link", "send_discount_nudge",
}


def get_amount(record, record_type: str) -> float:
    """checkout_abandonments stores cart_value, not amount — this is the one
    place that difference needs to be bridged, so no other code needs to know
    about it."""
    return record["cart_value"] if record_type == "abandonment" else record["amount"]


def execute_decision(decision_row, record, now: str) -> dict:
    """Returns dict with: outcome, new_status, audit_reasoning, audit_outcome, actor,
    recovered_amount (the true net amount recovered — equals `amount` for every action
    except a recovered send_discount_nudge, where it's net of DISCOUNT_PCT; None/not
    meaningful unless new_status == "recovered")."""
    action = decision_row["action"]
    record_id = decision_row["record_id"]
    record_type = decision_row["record_type"]
    amount = get_amount(record, record_type)
    reasoning = decision_row["reasoning"]
    recovered_amount = None

    if action == "retry_payment":
        failure_code = record["failure_code"] if record_type == "transaction" else None
        result = mock_razorpay_retry(record_id, amount, failure_code)
        if result["status"] == "success":
            outcome = f"Retry succeeded — ₹{amount:,.2f} recovered via {record['payment_method']}."
            new_status = "recovered"
        else:
            outcome = "Retry failed — payment still not collected."
            new_status = "recovering" if record["attempt_count"] + 1 < 3 else "lost"
        actor = "agent"

    elif action == "send_update_link":
        result = mock_outreach_send(record_id, "sms", action)
        if result["follow_up_payment_received"]:
            outcome = f"Update link sent and used — customer updated card, ₹{amount:,.2f} recovered."
            new_status = "recovered"
        else:
            outcome = "Update link sent and delivered — no follow-up payment yet."
            new_status = "recovering"
        actor = "agent"

    elif action == "send_reminder":
        result = mock_outreach_send(record_id, "email", action)
        if result["follow_up_payment_received"]:
            outcome = f"Reminder sent — invoice paid, ₹{amount:,.2f} recovered."
            new_status = "recovered"
        else:
            outcome = "Reminder sent and delivered — no payment yet."
            new_status = "recovering"
        actor = "agent"

    elif action == "escalate_reminder":
        result = mock_outreach_send(record_id, "whatsapp", action)
        if result["follow_up_payment_received"]:
            outcome = f"Escalated reminder sent — invoice paid, ₹{amount:,.2f} recovered."
            new_status = "recovered"
        else:
            outcome = "Escalated reminder sent and delivered — no payment yet."
            new_status = "recovering"
        actor = "agent"

    elif action == "send_cart_recovery_link":
        result = mock_outreach_send(record_id, "sms", action)
        if result["follow_up_payment_received"]:
            outcome = f"Cart recovery link sent and used — checkout completed, ₹{amount:,.2f} recovered."
            new_status = "recovered"
        else:
            outcome = "Cart recovery link sent and delivered — no follow-up checkout yet."
            new_status = "recovering"
        actor = "agent"

    elif action == "send_discount_nudge":
        result = mock_outreach_send(record_id, "email", action)
        if result["follow_up_payment_received"]:
            recovered_amount = amount * (1 - DISCOUNT_PCT)
            outcome = (
                f"Discount nudge sent and used — checkout completed, ₹{recovered_amount:,.2f} "
                f"recovered after a {DISCOUNT_PCT * 100:.0f}% discount (cart value was ₹{amount:,.2f})."
            )
            new_status = "recovered"
        else:
            outcome = "Discount nudge sent and delivered — no follow-up checkout yet."
            new_status = "recovering"
        actor = "agent"

    elif action == "escalate_to_human":
        outcome = "Ticket logged for human review — no automated outcome."
        new_status = "escalated"
        actor = "agent"  # the *escalation* is an agent action; the human hasn't acted yet

    elif action == "stop_no_action":
        outcome = "Closed with no further action, per stopping rule."
        if decision_row["stopping_rule_fired"] == "cooldown_24h":
            # temporary hold, not terminal — record stays in its current state
            new_status = record["status"]
        else:
            # e.g. customer_opt_out — a compliance-driven closure, distinct from
            # a genuinely failed recovery attempt. Must not be conflated with "lost".
            new_status = "stopped"
        actor = "agent"

    else:
        outcome = "Unknown action — no-op."
        new_status = record["status"]
        actor = "agent"

    # Every recovered action other than a successful send_discount_nudge
    # recovers the full `amount` (the discount branch above already set its
    # own net-of-discount recovered_amount before we get here).
    if new_status == "recovered" and recovered_amount is None:
        recovered_amount = amount

    return {
        "outcome": outcome,
        "new_status": new_status,
        "reasoning": reasoning,
        "actor": actor,
        "action": action,
        "recovered_amount": recovered_amount,
    }


def run_execution(types=("transaction", "receivable", "abandonment")):
    conn = get_connection()
    cur = conn.cursor()
    now = datetime.now().isoformat(timespec="seconds")

    txn_by_id = {r["transaction_id"]: r for r in cur.execute("SELECT * FROM transactions").fetchall()}
    inv_by_id = {r["invoice_id"]: r for r in cur.execute("SELECT * FROM receivables").fetchall()}
    ab_by_id = {r["session_id"]: r for r in cur.execute("SELECT * FROM checkout_abandonments").fetchall()}
    decisions = [d for d in cur.execute("SELECT * FROM decisions").fetchall() if d["record_type"] in types]

    cur.execute("DELETE FROM audit_log WHERE record_type IN ({})".format(",".join("?" * len(types))), types)

    # bucket counters for the consistency check
    bucket_counts = {"recovered": 0, "escalated": 0, "still_failing": 0, "stopped_no_action": 0}
    recovered_amount = 0.0
    total_amount = 0.0
    by_failure_code = {}  # failure_code -> {"total": n, "at_risk": ₹, "recovered": n, "recovered_amt": ₹}
    stopping_rule_triggers = 0
    human_escalations = 0
    txn_total = txn_recovered = 0.0
    recv_total = recv_recovered = 0.0
    aband_total = aband_recovered = 0.0

    audit_rows = []

    for d in decisions:
        record = txn_by_id.get(d["record_id"]) or inv_by_id.get(d["record_id"]) or ab_by_id.get(d["record_id"])
        record_type = d["record_type"]
        amount = get_amount(record, record_type)
        total_amount += amount

        result = execute_decision(d, record, now)
        action = result["action"]

        # --- bucket classification (mutually exclusive, covers every record exactly once) ---
        if record_type == "transaction":
            txn_total += amount
        elif record_type == "receivable":
            recv_total += amount
        else:
            aband_total += amount

        if result["new_status"] == "recovered":
            bucket = "recovered"
            net_amount = result["recovered_amount"]  # == amount for every action except a discount-nudge recovery
            recovered_amount += net_amount
            if record_type == "transaction":
                txn_recovered += net_amount
            elif record_type == "receivable":
                recv_recovered += net_amount
            else:
                aband_recovered += net_amount
        elif action == "escalate_to_human":
            bucket = "escalated"
            human_escalations += 1
        elif action == "stop_no_action":
            bucket = "stopped_no_action"
        else:
            bucket = "still_failing"
        bucket_counts[bucket] += 1

        if d["stopping_rule_fired"]:
            stopping_rule_triggers += 1

        # --- failure_code breakdown (transactions only; unaffected by the
        # discount-recovery fix since transactions have no discount action) ---
        if record_type == "transaction":
            fc = record["failure_code"]
            by_failure_code.setdefault(fc, {"total": 0, "at_risk": 0.0, "recovered": 0, "recovered_amt": 0.0})
            by_failure_code[fc]["total"] += 1
            by_failure_code[fc]["at_risk"] += amount
            if bucket == "recovered":
                by_failure_code[fc]["recovered"] += 1
                by_failure_code[fc]["recovered_amt"] += amount

        # --- persist status update on the source record ---
        table = {"transaction": "transactions", "receivable": "receivables", "abandonment": "checkout_abandonments"}[record_type]
        id_col = {"transaction": "transaction_id", "receivable": "invoice_id", "abandonment": "session_id"}[record_type]
        new_attempt_count = record["attempt_count"] + (1 if action in AUTOMATED_OUTCOME_ACTIONS else 0)
        if record_type == "abandonment":
            # abandonments alone carry a recovered_amount column (net-of-discount
            # accounting fix) — stays NULL unless this record just recovered.
            cur.execute(
                "UPDATE checkout_abandonments SET status = ?, attempt_count = ?, "
                "last_action_timestamp = ?, recovered_amount = ? WHERE session_id = ?",
                (result["new_status"], new_attempt_count, now, result["recovered_amount"], d["record_id"]),
            )
        else:
            cur.execute(
                f"UPDATE {table} SET status = ?, attempt_count = ?, last_action_timestamp = ? WHERE {id_col} = ?",
                (result["new_status"], new_attempt_count, now, d["record_id"]),
            )

        audit_rows.append((
            d["record_id"], record_type, now, action, result["reasoning"], result["outcome"], result["actor"],
        ))

    cur.executemany(
        """INSERT INTO audit_log (record_id, record_type, timestamp, action_taken, reasoning, outcome, actor)
           VALUES (?,?,?,?,?,?,?)""",
        audit_rows,
    )
    conn.commit()

    # --- CONSISTENCY ASSERTION ---
    total_processed = sum(bucket_counts.values())
    assert total_processed == len(decisions), (
        f"Consistency check FAILED: bucketed {total_processed} but processed {len(decisions)}"
    )
    assert (bucket_counts["recovered"] + bucket_counts["escalated"] +
            bucket_counts["still_failing"] + bucket_counts["stopped_no_action"]) == len(decisions)
    print(f"✅ CONSISTENCY CHECK PASSED: recovered({bucket_counts['recovered']}) + "
          f"escalated({bucket_counts['escalated']}) + still_failing({bucket_counts['still_failing']}) + "
          f"stopped_no_action({bucket_counts['stopped_no_action']}) = {total_processed} "
          f"== total transactions processed ({len(decisions)})\n")

    # --- FINAL SUMMARY ---
    print("=" * 70)
    print("FINAL PIPELINE SUMMARY")
    print("=" * 70)
    print(f"\nTotal records processed:     {len(decisions)}")
    print(f"Total revenue at risk:       ₹{total_amount:,.2f}")
    print(f"Total revenue recovered:     ₹{recovered_amount:,.2f}")
    print(f"Overall recovery rate (blended): {recovered_amount/total_amount*100:.1f}%")
    if txn_total:
        print(f"  Transactions-only recovery rate:  {txn_recovered/txn_total*100:.1f}%  (₹{txn_recovered:,.2f} / ₹{txn_total:,.2f})")
    if recv_total:
        print(f"  Receivables-only recovery rate:   {recv_recovered/recv_total*100:.1f}%  (₹{recv_recovered:,.2f} / ₹{recv_total:,.2f})")
    if aband_total:
        print(f"  Abandonments-only recovery rate:  {aband_recovered/aband_total*100:.1f}%  (₹{aband_recovered:,.2f} / ₹{aband_total:,.2f})")
    print(f"\nOutcome buckets:")
    for k, v in bucket_counts.items():
        print(f"  {k:<20} {v}")
    print(f"\nStopping-rule triggers:      {stopping_rule_triggers}")
    print(f"Human escalations:            {human_escalations}")

    print("\nRecovery rate by failure_code (transactions only):")
    for fc, stats in sorted(by_failure_code.items(), key=lambda x: -x[1]["at_risk"]):
        rate = stats["recovered"] / stats["total"] * 100 if stats["total"] else 0
        print(f"  {fc:<20} {stats['recovered']}/{stats['total']:<4} recovered "
              f"({rate:5.1f}%)   ₹{stats['recovered_amt']:,.2f} / ₹{stats['at_risk']:,.2f}")

    # --- 3 sample audit trails, full chain ---
    print("\n" + "=" * 70)
    print("SAMPLE AUDIT TRAILS (diagnosis -> decision -> execution -> outcome)")
    print("=" * 70)

    diag_by_id = {d["record_id"]: d for d in cur.execute("SELECT * FROM diagnoses").fetchall()}
    dec_by_id = {d["record_id"]: d for d in decisions}

    sample_ids = []
    if decisions:
        n = len(decisions)
        for frac in (0.05, 0.3, 0.9):
            idx = min(int(n * frac), n - 1)
            rid = decisions[idx]["record_id"]
            if rid not in sample_ids:
                sample_ids.append(rid)
    for i, rid in enumerate(sample_ids, 1):
        diag = diag_by_id[rid]
        dec = dec_by_id[rid]
        log = cur.execute("SELECT * FROM audit_log WHERE record_id = ?", (rid,)).fetchone()
        print(f"\n[{i}] {diag['record_type'].upper()} — {rid}")
        print(f"    1. DIAGNOSIS:  {diag['root_cause']}")
        print(f"                   (confidence={diag['confidence']}, risk_flag={bool(diag['risk_flag'])}, urgency={diag['recommended_urgency']})")
        print(f"    2. DECISION:   action={dec['action']}"
              + (f", stopping_rule={dec['stopping_rule_fired']}" if dec["stopping_rule_fired"] else ""))
        print(f"    3. EXECUTION:  {log['outcome']}")
        print(f"    4. ACTOR:      {log['actor']}")

    conn.close()


if __name__ == "__main__":
    run_execution()
