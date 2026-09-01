"""
Loop 1: Synthetic data generator.

Produces (per pipeline run — see populate()):
  - 200 failed payment transactions, weighted realistically across failure codes
  - 400 overdue B2B receivables, days_overdue spread 7-90
  - 200 checkout abandonments
Amounts follow a realistic bimodal distribution: small consumer payments
(₹200-5,000) vs larger B2B-style invoices (₹10,000-500,000).

Phase 2 (durable audit trail): populate() no longer wipes prior data on each
run — every row it inserts is tagged with a fresh batch_id (see
schema.create_batch()), so re-running the pipeline is additive. NOTE (honesty
disclosure): random.seed(42) below is fixed at import time for reproducible
demo data, which means the *distributional* shape of each new batch's
amounts/failure-codes/days-overdue is identical run to run (only the UUIDs
and the datetime.now()-relative timestamps genuinely differ) — this was a
reasonable choice when every run started from an empty table, and remains
harmless for Phase 2's purpose (proving batches persist and are
independently queryable), but it does mean two batches won't look like two
independently-drawn samples if someone inspects the underlying distribution
closely. Left as-is rather than silently "fixed" out of scope for this
phase; a real re-seed-per-batch would be a one-line follow-up.

Phase 3 (promise-to-pay tracker): gen_receivables() also simulates realistic
promise-to-pay history for a disclosed, hand-set fraction of 45+ day overdue
invoices — see the inline comment in gen_receivables() for the exact
weights.
"""

import random
import uuid
from datetime import datetime, timedelta

from schema import get_connection, init_db, create_batch

random.seed(42)  # reproducible demo data

# Failure code weights reflect real-world payment failure distributions:
# insufficient_funds and bank_timeout dominate; risk_block and otp_failed are rarer.
FAILURE_CODE_WEIGHTS = {
    "insufficient_funds": 0.32,
    "bank_timeout": 0.24,
    "issuer_decline": 0.16,
    "expired_card": 0.12,
    "otp_failed": 0.10,
    "risk_block": 0.06,
}

PAYMENT_METHODS = ["card", "UPI", "netbanking", "mandate"]
# UPI rarely has "expired_card" as a failure reason - keep method/failure pairing plausible
METHOD_FAILURE_COMPAT = {
    "card": ["insufficient_funds", "bank_timeout", "issuer_decline", "expired_card", "otp_failed", "risk_block"],
    "UPI": ["insufficient_funds", "bank_timeout", "issuer_decline", "otp_failed", "risk_block"],
    "netbanking": ["insufficient_funds", "bank_timeout", "issuer_decline", "risk_block"],
    "mandate": ["insufficient_funds", "bank_timeout", "issuer_decline"],
}


def weighted_choice(weights: dict):
    codes, probs = zip(*weights.items())
    return random.choices(codes, weights=probs, k=1)[0]


def random_timestamp_within_days(days_back: int) -> str:
    dt = datetime.now() - timedelta(
        days=random.uniform(0, days_back),
        hours=random.uniform(0, 24),
    )
    return dt.isoformat(timespec="seconds")


def generate_transaction_amount() -> float:
    # 85% small consumer payments, 15% larger ones (still consumer-scale, not B2B)
    if random.random() < 0.85:
        return round(random.uniform(200, 5000), 2)
    return round(random.uniform(5000, 15000), 2)


def gen_transactions(n: int = 200):
    rows = []
    for _ in range(n):
        method = random.choice(PAYMENT_METHODS)
        compatible_codes = METHOD_FAILURE_COMPAT[method]
        # re-weight only over compatible codes
        sub_weights = {c: FAILURE_CODE_WEIGHTS[c] for c in compatible_codes}
        failure_code = weighted_choice(sub_weights)

        # attempt_count: most transactions are fresh, some have been retried already
        attempt_count = random.choices([0, 1, 2, 3], weights=[0.55, 0.25, 0.13, 0.07])[0]

        # customer_opt_out: small minority
        opt_out = 1 if random.random() < 0.05 else 0

        status = "open"
        if attempt_count >= 3:
            status = random.choice(["escalated", "lost"])

        rows.append((
            f"txn_{uuid.uuid4().hex[:10]}",
            f"cust_{uuid.uuid4().hex[:8]}",
            generate_transaction_amount(),
            "INR",
            method,
            failure_code,
            random_timestamp_within_days(30),
            attempt_count,
            status,
            opt_out,
            random_timestamp_within_days(5) if attempt_count > 0 else None,
        ))
    return rows


def generate_receivable_amount() -> float:
    return round(random.uniform(10000, 500000), 2)


def gen_receivables(n: int = 400):
    rows = []
    for _ in range(n):
        days_overdue = random.randint(7, 90)
        due_date = (datetime.now() - timedelta(days=days_overdue)).date().isoformat()

        # contact_history grows with days_overdue (more overdue = more prior contacts)
        n_contacts = min(3, days_overdue // 20)
        contact_history = str([
            {"day": i * 15, "type": random.choice(["email", "call", "reminder"])}
            for i in range(n_contacts)
        ]).replace("'", '"')

        opt_out = 1 if random.random() < 0.03 else 0
        status = "open" if days_overdue < 60 else random.choice(["open", "escalated"])

        # Phase 3 (promise-to-pay tracker) — DISCLOSED, HAND-SET, same
        # convention as every other distribution in this file: for 45+ day
        # overdue invoices only (a real collections process would already
        # have made contact by then), ~30% already have a prior promise on
        # file. Of those, ~65% are BROKEN (the promised date has already
        # passed with the invoice still unpaid — the worse, more common
        # case, since a promise that's still pending would usually mean the
        # invoice isn't 45+ days overdue anymore by the time it's due) and
        # ~35% are still PENDING (the promised date hasn't arrived yet).
        promised_pay_date = None
        promise_status = "none"
        if days_overdue >= 45 and random.random() < 0.30:
            if random.random() < 0.65:
                promise_status = "broken"
                promised_pay_date = (datetime.now() - timedelta(days=random.randint(1, 14))).date().isoformat()
            else:
                promise_status = "pending"
                promised_pay_date = (datetime.now() + timedelta(days=random.randint(1, 10))).date().isoformat()

        rows.append((
            f"inv_{uuid.uuid4().hex[:10]}",
            f"cust_{uuid.uuid4().hex[:8]}",
            generate_receivable_amount(),
            due_date,
            days_overdue,
            contact_history,
            status,
            opt_out,
            n_contacts,
            random_timestamp_within_days(days_overdue) if n_contacts > 0 else None,
            promised_pay_date,
            promise_status,
        ))
    return rows


def generate_cart_value() -> float:
    # same consumer-scale distribution as transactions, since abandoned carts
    # are pre-payment consumer checkouts, not B2B invoices
    if random.random() < 0.85:
        return round(random.uniform(200, 5000), 2)
    return round(random.uniform(5000, 15000), 2)


def gen_checkout_abandonments(n: int = 200):
    """Time-since-abandonment is the key axis (warm <1hr, cooling 1-24hr,
    cold >24hr) — see diagnosis.py for how this drives root-cause bucketing.
    Hand-set weights, disclosed as such, same convention as gen_transactions/
    gen_receivables above."""
    rows = []
    # 35% warm (<1hr), 40% cooling (1-24hr), 25% cold (>24hr) — hand-set weights
    for _ in range(n):
        bucket = random.choices(["warm", "cooling", "cold"], weights=[0.35, 0.40, 0.25])[0]
        if bucket == "warm":
            minutes_ago = random.uniform(1, 59)
        elif bucket == "cooling":
            minutes_ago = random.uniform(60, 24 * 60)
        else:
            minutes_ago = random.uniform(24 * 60, 7 * 24 * 60)
        abandoned_at = (datetime.now() - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")

        # payment_attempted: never-started vs attempted-then-left. Carts that
        # got as far as a payment attempt skew slightly higher-value (more
        # invested in the purchase already).
        payment_attempted = 1 if random.random() < 0.40 else 0
        cart_value = generate_cart_value()
        if payment_attempted and random.random() < 0.5:
            cart_value = round(cart_value * random.uniform(1.1, 1.6), 2)

        opt_out = 1 if random.random() < 0.05 else 0

        # attempt_count: most abandonments are fresh (no recovery attempt yet)
        attempt_count = random.choices([0, 1, 2, 3], weights=[0.65, 0.20, 0.10, 0.05])[0]
        status = "open"
        if attempt_count >= 3:
            status = random.choice(["escalated", "lost"])

        rows.append((
            f"sess_{uuid.uuid4().hex[:10]}",
            f"cust_{uuid.uuid4().hex[:8]}",
            cart_value,
            abandoned_at,
            payment_attempted,
            opt_out,
            attempt_count,
            random_timestamp_within_days(1) if attempt_count > 0 else None,
            status,
        ))
    return rows


def populate():
    # Phase 2: no longer wipes existing data (schema.init_db(reset=True)) —
    # each run is now ADDITIVE. init_db(reset=False) just ensures the schema
    # exists (safe/idempotent on an already-migrated DB); every row this run
    # creates is tagged with a fresh batch_id so it's independently
    # queryable later without disturbing prior batches. See
    # schema.create_batch()/get_current_batch_id().
    init_db(reset=False)
    conn = get_connection()
    cur = conn.cursor()

    txns = gen_transactions(200)
    recv = gen_receivables(400)
    abandon = gen_checkout_abandonments(200)

    batch_id = create_batch(conn, {
        "transactions": len(txns), "receivables": len(recv), "checkout_abandonments": len(abandon),
    })

    cur.executemany(
        """INSERT INTO transactions
           (transaction_id, customer_id, amount, currency, payment_method,
            failure_code, failure_timestamp, attempt_count, status,
            customer_opt_out, last_action_timestamp, batch_id)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        [row + (batch_id,) for row in txns],
    )

    cur.executemany(
        """INSERT INTO receivables
           (invoice_id, customer_id, amount, due_date, days_overdue,
            contact_history, status, customer_opt_out, attempt_count,
            last_action_timestamp, promised_pay_date, promise_status, batch_id)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [row + (batch_id,) for row in recv],
    )

    cur.executemany(
        """INSERT INTO checkout_abandonments
           (session_id, customer_id, cart_value, abandoned_at, payment_attempted,
            customer_opt_out, attempt_count, last_action_timestamp, status, batch_id)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        [row + (batch_id,) for row in abandon],
    )

    conn.commit()

    # --- Summary (THIS batch only — the table is cumulative now, so a plain
    # unfiltered COUNT/SUM would include every prior run too) ---
    print("=" * 60)
    print("SYNTHETIC DATA GENERATED")
    print(f"batch_id: {batch_id}")
    print("=" * 60)

    total_txn = cur.execute(
        "SELECT COUNT(*), SUM(amount) FROM transactions WHERE batch_id = ?", (batch_id,)
    ).fetchone()
    print(f"\nTransactions: {total_txn[0]}")
    print(f"Total ₹ at risk (transactions): ₹{total_txn[1]:,.2f}")

    print("\nBreakdown by failure_code:")
    for row in cur.execute(
        """SELECT failure_code, COUNT(*), SUM(amount)
           FROM transactions WHERE batch_id = ? GROUP BY failure_code ORDER BY COUNT(*) DESC""",
        (batch_id,),
    ):
        print(f"  {row[0]:<20} count={row[1]:<5} ₹{row[2]:,.2f}")

    print("\nBreakdown by payment_method:")
    for row in cur.execute(
        """SELECT payment_method, COUNT(*) FROM transactions
           WHERE batch_id = ? GROUP BY payment_method ORDER BY COUNT(*) DESC""",
        (batch_id,),
    ):
        print(f"  {row[0]:<15} count={row[1]}")

    total_recv = cur.execute(
        "SELECT COUNT(*), SUM(amount) FROM receivables WHERE batch_id = ?", (batch_id,)
    ).fetchone()
    print(f"\nReceivables (B2B invoices): {total_recv[0]}")
    print(f"Total ₹ at risk (receivables): ₹{total_recv[1]:,.2f}")

    print("\nBreakdown by overdue bucket:")
    for label, lo, hi in [("7-15 days", 7, 15), ("15-45 days", 15, 45), ("45-90 days", 45, 91)]:
        row = cur.execute(
            "SELECT COUNT(*), SUM(amount) FROM receivables WHERE batch_id = ? AND days_overdue >= ? AND days_overdue < ?",
            (batch_id, lo, hi),
        ).fetchone()
        cnt = row[0] or 0
        amt = row[1] or 0
        print(f"  {label:<12} count={cnt:<4} ₹{amt:,.2f}")

    total_aband = cur.execute(
        "SELECT COUNT(*), SUM(cart_value) FROM checkout_abandonments WHERE batch_id = ?", (batch_id,)
    ).fetchone()
    print(f"\nCheckout abandonments: {total_aband[0]}")
    print(f"Total ₹ at risk (abandonments): ₹{total_aband[1]:,.2f}")

    print("\nBreakdown by time-since-abandonment:")
    now_iso = datetime.now()
    for label, lo_min, hi_min in [("warm (<1hr)", 0, 60), ("cooling (1-24hr)", 60, 24 * 60), ("cold (>24hr)", 24 * 60, 10**9)]:
        cnt = 0
        amt = 0.0
        for row in cur.execute("SELECT cart_value, abandoned_at FROM checkout_abandonments WHERE batch_id = ?", (batch_id,)):
            mins = (now_iso - datetime.fromisoformat(row[1])).total_seconds() / 60
            if lo_min <= mins < hi_min:
                cnt += 1
                amt += row[0]
        print(f"  {label:<18} count={cnt:<4} ₹{amt:,.2f}")

    grand_total = (total_txn[1] or 0) + (total_recv[1] or 0) + (total_aband[1] or 0)
    print(f"\nGRAND TOTAL ₹ AT RISK: ₹{grand_total:,.2f}")
    print("=" * 60)

    conn.close()
    return batch_id


if __name__ == "__main__":
    populate()
