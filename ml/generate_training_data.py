"""
ML Phase A, step 1: training data generator.

WHY THIS EXISTS: our main pipeline only ever executes ONE decided action per
transaction, so we only observe the outcome of the action we chose — never the
counterfactual "what if we'd tried X instead". To train a model that can compare
candidate actions, we need labeled (context, candidate_action, outcome) triples
covering the space of plausible actions per failure type. This script simulates
that space directly: for every synthetic case, every action applicable to its
failure_code/overdue-tier/recency-tier is evaluated with its own simulated
outcome.

HONESTY NOTE: base success rates below are the same domain assumptions already
encoded in backend/execution.py (e.g. bank_timeout retries succeed more than
insufficient_funds retries). We layer small, realistic feature-driven modifiers
on top (time-of-day, attempt fatigue, invoice age, time-since-abandonment,
time-since-last-attempt) plus noise, so the model has to learn a real (if
modest) signal rather than just memorizing one constant per (failure_code,
action) pair. This is training on our own simulator's assumptions, not on
discovered real-world data — that's disclosed in DATA_SOURCES.md.

NO LEAKAGE: every feature used here is something known BEFORE the action is
taken. Nothing derived from the outcome itself is used as a feature.

FEATURES (Phase 3 additions marked): alongside the original context features
(failure_code/action, amount, attempt_count, hour, dow, days_overdue), two
genuinely new signals were added rather than constants-in-disguise:
  - hours_since_last_attempt: -1 sentinel when there's no prior attempt
    (attempt_count == 0); otherwise a simulated gap since the last automated
    touch. A very recent re-contact (e.g. re-trying within a couple hours)
    performs worse than a longer gap — this is a distinct signal from
    attempt_count (fatigue from *how many* attempts) since it captures
    fatigue from *how recently*.
  - is_repeat_failure: 1 if attempt_count >= 1, else 0. Deliberately kept as
    an explicit categorical alongside the continuous attempt_count so
    tree-based models can split cleanly on "any prior attempt at all" instead
    of only learning a linear attempt_count effect. This is a light
    duplication of information already in attempt_count, not a fabricated
    signal — documented here rather than silently added.
Per the existing project rule, customer_id-derived "personalization" features
(tenure, prior success rate) are still NOT included, since every synthetic
customer_id is generated once and never repeats — those would be constants in
disguise, not real signal.
"""

import random
from pathlib import Path

import numpy as np
import pandas as pd

random.seed(7)
np.random.seed(7)

# Anchored to the repo root (not CWD) — same convention as backend/config.py's
# PROJECT_ROOT — so this script works whether it's run as `python3
# generate_training_data.py` from inside ml/, `python3 ml/generate_training_data.py`
# from the repo root (what docker-entrypoint.sh and the README both do), or
# from any other directory.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
TRAINING_DATA_PATH = PROJECT_ROOT / "ml" / "training_data.csv"

N_BASE_CASES = 3600

TXN_FAILURE_WEIGHTS = {
    "insufficient_funds": 0.32, "bank_timeout": 0.24, "issuer_decline": 0.16,
    "expired_card": 0.12, "otp_failed": 0.10, "risk_block": 0.06,
}

# Candidate actions considered per failure_code. risk_block has none — it is a
# hard policy block, never offered to the recovery-probability model at all.
ACTIONS_BY_FAILURE_CODE = {
    "insufficient_funds": ["RETRY_NOW", "RETRY_LATER"],
    "bank_timeout": ["RETRY_NOW", "RETRY_LATER"],
    "issuer_decline": ["RETRY_NOW", "RETRY_LATER", "ALTERNATE_PAYMENT_METHOD"],
    "otp_failed": ["RETRY_NOW"],
    "expired_card": ["REQUEST_PAYMENT_METHOD_UPDATE"],
}

RECEIVABLE_ACTIONS = ["SEND_REMINDER", "ESCALATE_REMINDER"]
ABANDONMENT_ACTIONS = ["SEND_CART_RECOVERY_LINK", "SEND_DISCOUNT_NUDGE"]

# Same policy gate as backend/candidate_actions.py — kept in sync so training
# outcomes are only ever simulated for actions the real system would offer.
DISCOUNT_NUDGE_MIN_CART_VALUE = 3000

# Base success rates — same domain story as backend/execution.py's mock tables.
BASE_RATE = {
    ("insufficient_funds", "RETRY_NOW"): 0.22,
    ("insufficient_funds", "RETRY_LATER"): 0.42,
    ("bank_timeout", "RETRY_NOW"): 0.65,
    ("bank_timeout", "RETRY_LATER"): 0.55,
    ("issuer_decline", "RETRY_NOW"): 0.42,
    ("issuer_decline", "RETRY_LATER"): 0.38,
    ("issuer_decline", "ALTERNATE_PAYMENT_METHOD"): 0.50,
    ("otp_failed", "RETRY_NOW"): 0.55,
    ("expired_card", "REQUEST_PAYMENT_METHOD_UPDATE"): 0.55,
}


def sample_hours_since_last_attempt(attempt_count: int, scale_hours: float) -> float:
    """-1 sentinel = no prior attempt. Otherwise draws a plausible gap; scale_hours
    sets the rough magnitude per record type (transactions retry within hours/
    days, receivables over days/weeks, abandonments within hours)."""
    if attempt_count == 0:
        return -1.0
    return float(round(np.random.exponential(scale_hours), 2))


def hours_since_last_attempt_modifier(hours_since_last_attempt: float) -> float:
    """Shared modifier: re-contacting too soon after a prior attempt performs
    worse (fatigue/annoyance); a longer gap recovers somewhat, with diminishing
    returns. -1 sentinel (no prior attempt) contributes no modifier."""
    if hours_since_last_attempt < 0:
        return 0.0
    if hours_since_last_attempt < 6:
        return -0.05
    if hours_since_last_attempt < 24:
        return -0.01
    return min(0.04, hours_since_last_attempt / 1000)


def simulate_txn_outcome(failure_code, action, amount, attempt_count, hour, dow, hours_since_last_attempt):
    p = BASE_RATE[(failure_code, action)]

    # attempt fatigue: each prior automated attempt slightly erodes the odds
    p -= 0.04 * attempt_count
    p += hours_since_last_attempt_modifier(hours_since_last_attempt)

    # business-hours modifier: outreach-adjacent actions (update link) land
    # better when sent during waking/working hours
    if action == "REQUEST_PAYMENT_METHOD_UPDATE":
        p += 0.06 if 9 <= hour <= 20 else -0.05

    # weekday vs weekend: retries during a weekday (salary-linked funds more
    # likely available, banking rails fully staffed) do marginally better
    if dow < 5:
        p += 0.02

    # amount: very large consumer payments are marginally harder to push through
    if amount > 4000:
        p -= 0.03

    p += np.random.normal(0, 0.03)  # noise
    return float(np.clip(p, 0.03, 0.97))


def simulate_abandonment_outcome(action, minutes_since_abandoned, attempt_count, hour, dow,
                                  payment_attempted, hours_since_last_attempt):
    # Base rates loosely mirror commerce cart-recovery benchmarks: a plain
    # recovery link converts modestly; a discount nudge converts better but
    # is only ever offered per the policy gate in candidate_actions.py.
    base = 0.20 if action == "SEND_CART_RECOVERY_LINK" else 0.30
    p = base

    # time decay: warm carts (<1hr) convert far better than cold ones. Capped
    # so a week-old cart still has some (low) residual chance rather than
    # being driven to the floor by the coefficient alone.
    p -= min(0.16, 0.00003 * minutes_since_abandoned)  # ~-0.16 by 24hr+, flat after

    # a customer who already attempted payment once is a warmer lead than one
    # who never started — they were closer to converting
    if payment_attempted:
        p += 0.08

    # fatigue from repeated recovery attempts
    p -= 0.04 * attempt_count
    p += hours_since_last_attempt_modifier(hours_since_last_attempt)

    # business/evening hours (typical online shopping windows) convert better
    if 10 <= hour <= 22:
        p += 0.03

    p += np.random.normal(0, 0.03)
    return float(np.clip(p, 0.02, 0.95))


def simulate_receivable_outcome(action, days_overdue, attempt_count, hour, dow, hours_since_last_attempt):
    base = 0.30 if action == "SEND_REMINDER" else 0.35
    p = base

    # older invoices are harder to collect regardless of action
    p -= 0.002 * days_overdue

    # fatigue from repeated contact attempts
    p -= 0.03 * attempt_count
    p += hours_since_last_attempt_modifier(hours_since_last_attempt)

    # business hours / weekday outreach performs better for B2B contacts
    if 9 <= hour <= 18 and dow < 5:
        p += 0.05

    p += np.random.normal(0, 0.03)
    return float(np.clip(p, 0.02, 0.95))


def weighted_choice(weights):
    codes, probs = zip(*weights.items())
    return random.choices(codes, weights=probs, k=1)[0]


def generate():
    rows = []

    for _ in range(N_BASE_CASES):
        record_kind = random.choices(
            ["transaction", "receivable", "abandonment"], weights=[0.65, 0.15, 0.20]
        )[0]

        hour = random.randint(0, 23)
        dow = random.randint(0, 6)
        attempt_count = random.choices([0, 1, 2], weights=[0.6, 0.28, 0.12])[0]
        is_repeat_failure = int(attempt_count >= 1)

        if record_kind == "abandonment":
            # matches the warm/cooling/cold weighting used in
            # backend/generate_data.py's gen_checkout_abandonments, so the
            # training distribution reflects the same real-world skew toward
            # recent abandonments rather than a flat week-long uniform draw.
            recency_bucket = random.choices(["warm", "cooling", "cold"], weights=[0.35, 0.40, 0.25])[0]
            if recency_bucket == "warm":
                minutes_since_abandoned = random.uniform(1, 59)
            elif recency_bucket == "cooling":
                minutes_since_abandoned = random.uniform(60, 24 * 60)
            else:
                minutes_since_abandoned = random.uniform(24 * 60, 7 * 24 * 60)
            payment_attempted = 1 if random.random() < 0.4 else 0
            cart_value = round(random.uniform(200, 15000), 2)
            hours_since_last_attempt = sample_hours_since_last_attempt(attempt_count, scale_hours=6)
            for action in ABANDONMENT_ACTIONS:
                if action == "SEND_DISCOUNT_NUDGE" and not (cart_value > DISCOUNT_NUDGE_MIN_CART_VALUE and attempt_count >= 1):
                    continue  # respect the same policy gate as candidate_actions.py at training time
                p = simulate_abandonment_outcome(
                    action, minutes_since_abandoned, attempt_count, hour, dow,
                    payment_attempted, hours_since_last_attempt,
                )
                outcome = 1 if random.random() < p else 0
                rows.append({
                    "record_type": "abandonment", "failure_code": "no_failure_code", "action": action,
                    "amount": cart_value, "attempt_count": attempt_count, "hour": hour, "dow": dow,
                    "days_overdue": -1, "minutes_since_event": round(minutes_since_abandoned, 1),
                    "payment_attempted": payment_attempted,
                    "hours_since_last_attempt": hours_since_last_attempt,
                    "is_repeat_failure": is_repeat_failure,
                    "outcome": outcome, "true_prob": p,
                })
            continue

        if record_kind == "receivable":
            days_overdue = random.randint(7, 90)
            amount = round(random.uniform(10000, 500000), 2)
            hours_since_last_attempt = sample_hours_since_last_attempt(attempt_count, scale_hours=96)
            for action in RECEIVABLE_ACTIONS:
                p = simulate_receivable_outcome(action, days_overdue, attempt_count, hour, dow, hours_since_last_attempt)
                outcome = 1 if random.random() < p else 0
                rows.append({
                    "record_type": "receivable", "failure_code": "no_failure_code", "action": action,
                    "amount": amount, "attempt_count": attempt_count, "hour": hour, "dow": dow,
                    "days_overdue": days_overdue, "minutes_since_event": -1, "payment_attempted": -1,
                    "hours_since_last_attempt": hours_since_last_attempt,
                    "is_repeat_failure": is_repeat_failure,
                    "outcome": outcome, "true_prob": p,
                })
        else:
            failure_code = weighted_choice(TXN_FAILURE_WEIGHTS)
            if failure_code == "risk_block":
                continue  # no candidate actions — always a hard policy block, not a training case
            amount = round(random.uniform(200, 15000), 2)
            hours_since_last_attempt = sample_hours_since_last_attempt(attempt_count, scale_hours=12)
            for action in ACTIONS_BY_FAILURE_CODE[failure_code]:
                p = simulate_txn_outcome(failure_code, action, amount, attempt_count, hour, dow, hours_since_last_attempt)
                outcome = 1 if random.random() < p else 0
                rows.append({
                    "record_type": "transaction", "failure_code": failure_code, "action": action,
                    "amount": amount, "attempt_count": attempt_count, "hour": hour, "dow": dow,
                    "days_overdue": -1, "minutes_since_event": -1, "payment_attempted": -1,
                    "hours_since_last_attempt": hours_since_last_attempt,
                    "is_repeat_failure": is_repeat_failure,
                    "outcome": outcome, "true_prob": p,
                })

    df = pd.DataFrame(rows)
    df.to_csv(TRAINING_DATA_PATH, index=False)
    print(f"Generated {len(df)} (context, candidate_action, outcome) training rows")
    print(f"Overall positive rate: {df['outcome'].mean():.3f}")
    print("\nBy record_type:")
    print(df.groupby("record_type")["outcome"].agg(["count", "mean"]).round(3))
    print("\nBy action:")
    print(df.groupby("action")["outcome"].agg(["count", "mean"]).round(3))
    return df


if __name__ == "__main__":
    generate()
