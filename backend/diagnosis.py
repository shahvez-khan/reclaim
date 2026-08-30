"""
Loop 2: Diagnosis agent.

Design choice: rules engine, not an LLM call. Reasoning: diagnosis logic here is
deterministic domain knowledge (failure_code -> root cause mapping, overdue
bucket -> outreach tier) rather than something requiring judgment over
unstructured text. A rules engine is faster to build correctly, 100% reproducible
for the demo, and every rule doubles as a plain-English audit line — which is
exactly what the compliance/explainability story needs. An LLM call remains a
drop-in swap later (see `llm_diagnose_stub` at the bottom) without touching the
decision/execution stages.
"""

import json
from datetime import datetime, timedelta

from schema import get_connection

FAILURE_ROOT_CAUSE = {
    "insufficient_funds": "Customer's account did not have enough balance at the time of the charge.",
    "expired_card": "The card on file has expired and can no longer be charged.",
    "bank_timeout": "The issuing bank did not respond in time — likely a transient network/infra issue, not a customer-side problem.",
    "otp_failed": "Customer failed OTP verification, possibly due to a UX issue, wrong OTP, or fraud attempt.",
    "risk_block": "Payment was blocked by risk/fraud controls, either issuer-side or on our end.",
    "issuer_decline": "The issuing bank declined the transaction without a specific reason surfaced — could be a policy block, limit breach, or false-positive fraud flag.",
}

# Confidence reflects how certain the root-cause mapping is, not the diagnosis's
# recovery odds. Bank/network-side failures with objective codes get higher
# confidence than declines with vague/absent reason codes.
FAILURE_CONFIDENCE = {
    "insufficient_funds": 0.90,
    "expired_card": 0.97,
    "bank_timeout": 0.85,
    "otp_failed": 0.70,
    "risk_block": 0.75,
    "issuer_decline": 0.60,
}


def next_payday_like_date(from_dt: datetime) -> datetime:
    """Approximate next 'payday cluster': the 1st of next month or the last week
    of the current month, whichever comes first from from_dt."""
    # last-week-of-month candidate
    if from_dt.month == 12:
        next_month_start = from_dt.replace(year=from_dt.year + 1, month=1, day=1)
    else:
        next_month_start = from_dt.replace(month=from_dt.month + 1, day=1)
    last_week_start = next_month_start - timedelta(days=7)

    if from_dt < last_week_start:
        return last_week_start
    return next_month_start  # 1st of next month


def diagnose_transaction(row) -> dict:
    failure_code = row["failure_code"]
    attempt_count = row["attempt_count"]
    root_cause = FAILURE_ROOT_CAUSE[failure_code]
    confidence = FAILURE_CONFIDENCE[failure_code]

    risk_flag = False
    reasoning_extra = ""

    if failure_code == "risk_block":
        risk_flag = True
        reasoning_extra = " Flagged for human review because automated recovery on a risk block could look like retry abuse to the issuer."
    elif failure_code == "otp_failed" and attempt_count >= 3:
        risk_flag = True
        reasoning_extra = " Flagged for human review: 3+ OTP failures suggests possible fraud or a customer who is stuck, not a simple retry case."

    if failure_code == "insufficient_funds":
        failure_dt = datetime.fromisoformat(row["failure_timestamp"])
        payday = next_payday_like_date(failure_dt)
        urgency = "within_week"
        note = f" Recommend retrying around the next likely payday cluster ({payday.date().isoformat()}) rather than immediately."
    elif failure_code == "expired_card":
        urgency = "within_24h"
        note = " Recommend a card-update flow — retrying the same card will fail every time."
    elif failure_code in ("bank_timeout", "issuer_decline"):
        urgency = "immediate"
        note = " Recommend an immediate smart retry — this looks transient, not a hard customer-side block."
    elif failure_code == "otp_failed":
        urgency = "immediate" if not risk_flag else "within_24h"
        note = " Recommend a retry with a clearer OTP prompt, unless flagged for review above."
    elif failure_code == "risk_block":
        urgency = "within_24h"
        note = " No automated action — route to human risk review."
    else:
        urgency = "within_week"
        note = ""

    return {
        "record_id": row["transaction_id"],
        "record_type": "transaction",
        "root_cause": root_cause + note + reasoning_extra,
        "confidence": confidence,
        "risk_flag": risk_flag,
        "needs_manual_followup": False,  # transactions use risk_flag directly; this flag is receivables-specific
        "recommended_urgency": urgency,
    }


def diagnose_receivable(row) -> dict:
    days_overdue = row["days_overdue"]

    if days_overdue < 15:
        tier = "soft reminder"
        urgency = "within_week"
        confidence = 0.95
    elif days_overdue < 45:
        tier = "escalated reminder"
        urgency = "within_24h"
        confidence = 0.90
    else:
        tier = "promise-to-pay outreach"
        urgency = "immediate"
        confidence = 0.85

    # IMPORTANT: risk_flag is reserved for actual fraud/risk cases per the Loop 2
    # spec (risk_block, repeated OTP failures) — it HARD BLOCKS all automated
    # action. A 45+ day overdue invoice is not a risk/fraud case, it's just old;
    # it should still get the automated promise-to-pay outreach. We use a
    # separate needs_manual_followup flag so a human also gets eyes on it
    # WITHOUT blocking the automated outreach action — matching the spec's
    # "promise-to-pay outreach + flag for manual follow-up" (both, not either/or).
    risk_flag = False
    needs_manual_followup = days_overdue >= 45

    root_cause = (
        f"Invoice is {days_overdue} days overdue. "
        f"Recommended track: {tier}."
    )
    if needs_manual_followup:
        root_cause += " Also flagged for manual follow-up given how overdue this account is — but automated outreach still proceeds in parallel."

    return {
        "record_id": row["invoice_id"],
        "record_type": "receivable",
        "root_cause": root_cause,
        "confidence": confidence,
        "risk_flag": risk_flag,
        "needs_manual_followup": needs_manual_followup,
        "recommended_urgency": urgency,
    }


def diagnose_abandonment(row) -> dict:
    """Root cause bucket derived from payment_attempted + cart value relative
    to a rough 'typical' consumer-cart range (we don't have real per-customer
    history since customer_ids are single-use synthetic IDs, so this uses the
    dataset-wide typical range as a stand-in, same honesty stance as the
    ml README note about not faking personalization)."""
    abandoned_at = datetime.fromisoformat(row["abandoned_at"])
    minutes_ago = (datetime.now() - abandoned_at).total_seconds() / 60

    if minutes_ago < 60:
        recency_tier, urgency = "warm", "immediate"
    elif minutes_ago < 24 * 60:
        recency_tier, urgency = "cooling", "within_24h"
    else:
        recency_tier, urgency = "cold", "within_week"

    # "typical range" for price-hesitation framing — a hand-set consumer-cart
    # threshold (₹5,000), not derived from any per-customer history since
    # customer_ids never repeat in this synthetic dataset.
    TYPICAL_CART_CEILING = 5000

    if row["payment_attempted"]:
        bucket = "attempted and dropped"
        root_cause = (
            "Customer reached the payment step and left without completing it — "
            "likely a friction point in the payment flow itself (OTP, redirect, "
            "slow gateway) rather than a pricing objection."
        )
        confidence = 0.75
    elif row["cart_value"] > TYPICAL_CART_CEILING:
        bucket = "price hesitation"
        root_cause = (
            f"Cart value (₹{row['cart_value']:,.2f}) is above the typical consumer-cart "
            "range and payment was never attempted — consistent with price hesitation "
            "rather than a technical blocker."
        )
        confidence = 0.55  # softer signal than payment_attempted — it's an inference, not observed
    else:
        bucket = "never attempted payment"
        root_cause = (
            "Customer never reached the payment step for a normally-priced cart — "
            "could be simple distraction/browsing rather than a real purchase intent signal."
        )
        confidence = 0.55

    root_cause += f" Time since abandonment: {recency_tier} ({minutes_ago:.0f} min)."

    return {
        "record_id": row["session_id"],
        "record_type": "abandonment",
        "root_cause": root_cause,
        "confidence": confidence,
        "risk_flag": False,  # no fraud/risk signal modeled for abandonments
        "needs_manual_followup": False,
        "recommended_urgency": urgency,
        "root_cause_bucket": bucket,  # not persisted in schema; consumed by candidate_actions if useful
    }


def llm_diagnose_stub(record: dict) -> dict:
    """Drop-in replacement point: swap diagnose_transaction/diagnose_receivable
    for a real LLM call here later (e.g. Claude via the Anthropic API) without
    changing the decision/execution stages downstream — they only depend on the
    dict shape returned (root_cause, confidence, risk_flag, recommended_urgency)."""
    raise NotImplementedError("Not used in this build — rules engine is authoritative.")


def run_diagnosis():
    conn = get_connection()
    cur = conn.cursor()
    now = datetime.now().isoformat(timespec="seconds")

    diagnoses = []

    for row in cur.execute("SELECT * FROM transactions").fetchall():
        d = diagnose_transaction(row)
        diagnoses.append(d)

    for row in cur.execute("SELECT * FROM receivables").fetchall():
        d = diagnose_receivable(row)
        diagnoses.append(d)

    for row in cur.execute("SELECT * FROM checkout_abandonments").fetchall():
        d = diagnose_abandonment(row)
        diagnoses.append(d)

    cur.execute("DELETE FROM diagnoses")  # idempotent re-run
    cur.executemany(
        """INSERT INTO diagnoses
           (record_id, record_type, root_cause, confidence, risk_flag, needs_manual_followup, recommended_urgency, diagnosed_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        [
            (d["record_id"], d["record_type"], d["root_cause"], d["confidence"],
             int(d["risk_flag"]), int(d["needs_manual_followup"]), d["recommended_urgency"], now)
            for d in diagnoses
        ],
    )
    conn.commit()

    print("=" * 70)
    print(f"DIAGNOSIS COMPLETE — {len(diagnoses)} records diagnosed")
    print("=" * 70)

    total = len(diagnoses)
    risk_flagged = sum(1 for d in diagnoses if d["risk_flag"])
    print(f"\nTotal diagnosed: {total}")
    print(f"Risk-flagged (routed to human review): {risk_flagged} ({risk_flagged/total*100:.1f}%)")

    print("\nUrgency breakdown:")
    for urg in ("immediate", "within_24h", "within_week"):
        cnt = sum(1 for d in diagnoses if d["recommended_urgency"] == urg)
        print(f"  {urg:<15} {cnt}")

    print("\n" + "=" * 70)
    print("5 EXAMPLE DIAGNOSES")
    print("=" * 70)

    # Pick a spread: one of each interesting case if possible
    examples = []
    seen_codes = set()
    txn_rows = {r["transaction_id"]: r for r in cur.execute("SELECT * FROM transactions").fetchall()}
    for d in diagnoses:
        if d["record_type"] == "transaction":
            code = txn_rows[d["record_id"]]["failure_code"]
            if code not in seen_codes:
                seen_codes.add(code)
                examples.append(d)
        if len(examples) >= 4:
            break
    # add one receivable example
    for d in diagnoses:
        if d["record_type"] == "receivable":
            examples.append(d)
            break

    for i, d in enumerate(examples[:5], 1):
        print(f"\n[{i}] {d['record_type'].upper()} — {d['record_id']}")
        print(f"    Root cause: {d['root_cause']}")
        print(f"    Confidence: {d['confidence']}")
        print(f"    Risk flag:  {d['risk_flag']}")
        print(f"    Urgency:    {d['recommended_urgency']}")

    conn.close()


if __name__ == "__main__":
    run_diagnosis()
