"""
Candidate action engine (trimmed-scope Upgrades #5/#6/#7).

Generates the set of POLICY-ELIGIBLE candidate actions for a record, scores
each with the trained recovery-probability model, computes expected value
(amount x probability), and returns them ranked. This module does NOT decide
whether automated action is permitted at all (opt-out / risk_flag / max
attempts / cool-off) — that hard gating still lives in decision.py exactly as
before and is checked FIRST. This module only runs once a record has already
cleared those hard gates, and its only job is picking the best action AMONG
the ones policy allows — matching the "policy always overrides expected
value" requirement.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from ml.predict_recovery import predict_recovery_probability

CANDIDATE_ACTIONS_BY_FAILURE_CODE = {
    "insufficient_funds": ["RETRY_NOW", "RETRY_LATER"],
    "bank_timeout": ["RETRY_NOW", "RETRY_LATER"],
    "issuer_decline": ["RETRY_NOW", "RETRY_LATER", "ALTERNATE_PAYMENT_METHOD"],
    "otp_failed": ["RETRY_NOW"],
    "expired_card": ["REQUEST_PAYMENT_METHOD_UPDATE"],
    # risk_block has no entry — it never reaches this engine, the hard
    # risk_flag policy gate in decision.py routes it straight to a human.
}

# Maps the granular ML candidate action back onto the broad execution-layer
# vocabulary backend/execution.py and the dashboard already understand, so we
# get real ML-informed decisions without rewriting the execution/audit/UI
# action vocabulary end to end.
EXECUTION_ACTION_MAP = {
    "RETRY_NOW": "retry_payment",
    "RETRY_LATER": "retry_payment",
    "ALTERNATE_PAYMENT_METHOD": "retry_payment",
    "REQUEST_PAYMENT_METHOD_UPDATE": "send_update_link",
    "SEND_REMINDER": "send_reminder",
    "ESCALATE_REMINDER": "escalate_reminder",
    "SEND_CART_RECOVERY_LINK": "send_cart_recovery_link",
    "SEND_DISCOUNT_NUDGE": "send_discount_nudge",
}

# Cart value above which a discount nudge is even considered — below this, a
# discount would erode margin on a small cart for little marginal recovery lift.
DISCOUNT_NUDGE_MIN_CART_VALUE = 3000

# DISCLOSED, HAND-SET (same convention as the other probability/cost tables
# in this project — not derived from real data): the size of the discount
# offered by SEND_DISCOUNT_NUDGE. A customer who completes checkout after a
# discount nudge pays cart_value * (1 - DISCOUNT_PCT), not the full cart
# value — every place in the codebase that sums "recovered" revenue for
# abandonments must use that net figure for discount-nudge recoveries, not
# gross cart_value (see execution.py's recovered_amount handling).
DISCOUNT_PCT = 0.12


def candidates_for_transaction(failure_code: str) -> list[str]:
    return CANDIDATE_ACTIONS_BY_FAILURE_CODE.get(failure_code, [])


def candidates_for_receivable(days_overdue: int) -> list[str]:
    # POLICY eligibility gate: firmer language isn't permitted this early,
    # regardless of what expected value says. See module docstring.
    if days_overdue < 15:
        return ["SEND_REMINDER"]
    return ["SEND_REMINDER", "ESCALATE_REMINDER"]


def candidates_for_abandonment(cart_value: float, attempt_count: int) -> list[str]:
    """SEND_CART_RECOVERY_LINK is always eligible — it's the low-cost,
    no-margin-impact default. SEND_DISCOUNT_NUDGE is POLICY-gated (not just
    ML-ranked) behind: cart value above DISCOUNT_NUDGE_MIN_CART_VALUE AND at
    least one prior reminder attempt has already failed (attempt_count >= 1)
    — we don't want to discount every abandoned cart by default, only ones
    where a plain reminder already didn't work and the cart is big enough to
    justify the margin hit."""
    candidates = ["SEND_CART_RECOVERY_LINK"]
    if cart_value > DISCOUNT_NUDGE_MIN_CART_VALUE and attempt_count >= 1:
        candidates.append("SEND_DISCOUNT_NUDGE")
    return candidates


def score_candidates(candidates: list[str], *, amount: float, attempt_count: int,
                      hour: int, dow: int, failure_code: str = "no_failure_code",
                      record_type: str = "transaction", days_overdue: int = -1,
                      hours_since_last_attempt: float = -1) -> list[dict]:
    scored = []
    for action in candidates:
        p = predict_recovery_probability(
            action=action, amount=amount, attempt_count=attempt_count, hour=hour, dow=dow,
            failure_code=failure_code, record_type=record_type, days_overdue=days_overdue,
            hours_since_last_attempt=hours_since_last_attempt,
        )
        scored.append({
            "candidate_action": action,
            "probability": round(p, 4),
            "expected_value": round(amount * p, 2),
        })
    scored.sort(key=lambda x: -x["expected_value"])
    return scored
