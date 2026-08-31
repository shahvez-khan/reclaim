"""
Loop 3: Decision & Policy agent.

Priority order for every record (checked top to bottom, first match wins):
  1. customer_opt_out          -> stop_no_action        (no exceptions, ever)
  2. risk_flag from diagnosis  -> escalate_to_human      (never auto-retry a risk case)
  3. attempt_count >= 3        -> escalate_to_human      (max automated attempts hit)
  4. 24h cool-off active       -> stop_no_action          (cooldown_24h, temporary hold)
  5. otherwise                 -> normal recovery action based on diagnosis

This ordering IS the compliance guarantee: opt-out and risk-flag are checked
before anything else can fire, so no combination of rules can route around them.
"""

from datetime import datetime, timedelta
import json
import logging

from schema import get_connection, get_current_batch_id
from candidate_actions import (
    candidates_for_transaction, candidates_for_receivable, candidates_for_abandonment,
    score_candidates, EXECUTION_ACTION_MAP,
)
from logging_config import configure_logging
from config import COOLDOWN_HOURS, MAX_ATTEMPTS

configure_logging()
logger = logging.getLogger("revenue_recovery.decision")

AUTOMATED_ACTIONS = {
    "retry_payment", "send_update_link", "send_reminder", "escalate_reminder",
    "send_cart_recovery_link", "send_discount_nudge",
}


def hours_since(last_action_ts: str | None) -> float:
    """-1 sentinel (matches the ML feature convention) if there's no prior
    action timestamp at all."""
    if not last_action_ts:
        return -1.0
    delta = datetime.now() - datetime.fromisoformat(last_action_ts)
    return round(delta.total_seconds() / 3600, 2)


def next_payday_like_date(from_dt: datetime) -> datetime:
    if from_dt.month == 12:
        next_month_start = from_dt.replace(year=from_dt.year + 1, month=1, day=1)
    else:
        next_month_start = from_dt.replace(month=from_dt.month + 1, day=1)
    last_week_start = next_month_start - timedelta(days=7)
    return last_week_start if from_dt < last_week_start else next_month_start


def cooldown_active(last_action_ts: str | None) -> bool:
    if not last_action_ts:
        return False
    last = datetime.fromisoformat(last_action_ts)
    return (datetime.now() - last) < timedelta(hours=COOLDOWN_HOURS)


def _record_id(row, record_type: str) -> str:
    id_col = {"transaction": "transaction_id", "receivable": "invoice_id", "abandonment": "session_id"}[record_type]
    return row[id_col]


def hard_policy_gate(row, diag_row, record_type: str) -> dict | None:
    """The compliance floor EVERY strategy must respect — smart agent or dumb
    baseline alike. Returns None if the record is clear for some automated
    action; otherwise returns the terminal decision info (action + which rule
    fired). This is deliberately separated from action-SELECTION logic so a
    naive baseline can share the exact same safety floor as the ML/EV-driven
    agent — the fair comparison is "same compliance constraints, different
    action-picking intelligence," not "compliant agent vs non-compliant baseline."

    Every fire is logged at INFO with record_id/record_type/stopping_rule —
    this is deliberately structured so it could feed a compliance report
    directly from the log stream, without re-deriving anything from the DB.
    """
    if row["customer_opt_out"]:
        logger.info("policy_gate_fired", extra={
            "record_id": _record_id(row, record_type), "record_type": record_type,
            "stopping_rule": "customer_opt_out",
        })
        return {"action": "stop_no_action", "stopping_rule": "customer_opt_out",
                "note": "customer has opted out of automated outreach — no exceptions."}
    if diag_row["risk_flag"]:
        logger.info("policy_gate_fired", extra={
            "record_id": _record_id(row, record_type), "record_type": record_type, "stopping_rule": "risk_flag",
        })
        return {"action": "escalate_to_human", "stopping_rule": "risk_flag",
                "note": "diagnosis flagged this as a risk case — routed to human review, never auto-retried."}
    if row["attempt_count"] >= MAX_ATTEMPTS:
        logger.info("policy_gate_fired", extra={
            "record_id": _record_id(row, record_type), "record_type": record_type, "stopping_rule": "max_attempts",
        })
        return {"action": "escalate_to_human", "stopping_rule": "max_attempts",
                "note": f"{row['attempt_count']} automated attempts already made (max {MAX_ATTEMPTS})."}
    if cooldown_active(row["last_action_timestamp"]):
        logger.info("policy_gate_fired", extra={
            "record_id": _record_id(row, record_type), "record_type": record_type, "stopping_rule": "cooldown_24h",
        })
        return {"action": "stop_no_action", "stopping_rule": "cooldown_24h",
                "note": f"last outreach under {COOLDOWN_HOURS}h ago."}
    return None


def decide_transaction(txn_row, diag_row, exclude_actions=frozenset(), is_replan=False, attempt_number=1) -> dict:
    reasoning_parts = [diag_row["root_cause"]]

    # Rules 1-3 (opt-out / risk / max-attempts) always apply, replan or not —
    # shared with baseline.py via hard_policy_gate() so both strategies sit
    # behind the identical compliance floor. Only cool-off (rule 4, below)
    # has replan-specific relaxation, so it's handled separately.
    gate = hard_policy_gate(txn_row, diag_row, "transaction")
    if gate:
        reasoning_parts.append(f"{'STOPPED' if gate['action']=='stop_no_action' else 'ESCALATED'}: {gate['note']}")
        return _decision(txn_row["transaction_id"], "transaction", gate["action"],
                          reasoning_parts, None, gate["stopping_rule"], attempt_number=attempt_number)

    # Rule 4: cool-off. On the FIRST decision of a fresh run this is checked
    # uniformly across every action type (a real prior contact happened, we
    # don't yet know when we'll run again, so we hold). On a WITHIN-SESSION
    # re-plan (is_replan=True) we only let it block a CUSTOMER-FACING outreach
    # action — a silent backend retry never "contacts" the customer, so it
    # doesn't need to respect a customer-contact cool-off. See module docstring.
    cooldown_now = cooldown_active(txn_row["last_action_timestamp"])
    if cooldown_now and not is_replan:
        reasoning_parts.append(f"HELD: last outreach to this customer was under {COOLDOWN_HOURS}h ago — holding to respect the cool-off period, not stacking actions.")
        return _decision(txn_row["transaction_id"], "transaction", "stop_no_action",
                          reasoning_parts, None, "cooldown_24h", attempt_number=attempt_number)

    # Normal path — ML-scored candidate actions, ranked by expected value.
    # Policy has already cleared this record to receive SOME automated action;
    # this only decides WHICH one among what's eligible for its failure_code.
    failure_code = txn_row["failure_code"]
    candidates = [a for a in candidates_for_transaction(failure_code) if a not in exclude_actions]

    if not candidates:
        # STOPPING RULE (not from hard_policy_gate): candidate exhaustion.
        # This is a DIFFERENT mechanism from hard_policy_gate's 4 rules
        # (opt_out/risk_flag/max_attempts/cooldown) — those fire BEFORE any
        # candidate is even generated, based on record-level policy state.
        # This one fires AFTER candidate generation, when the failure_code
        # either never had more than one candidate to begin with (e.g.
        # otp_failed -> only RETRY_NOW) or every candidate has already been
        # tried across re-plan attempts. Both are real, distinct reasons a
        # record ends up escalated to a human — both must be visible in the
        # "Stopping Rules Fired" panel, or that panel's claim ("every rule
        # below blocked an automated action from firing") is false for
        # whatever fraction of escalations land here instead of in
        # hard_policy_gate. See README's Testing section.
        reason = "no candidate actions remain untried" if exclude_actions else "no automated path defined for this failure code"
        reasoning_parts.append(f"ACTION: {reason} — routing to human.")
        return _decision(txn_row["transaction_id"], "transaction", "escalate_to_human",
                          reasoning_parts, None, "candidates_exhausted", attempt_number=attempt_number)

    failure_dt = datetime.fromisoformat(txn_row["failure_timestamp"])
    scored = score_candidates(
        candidates, amount=txn_row["amount"], attempt_count=txn_row["attempt_count"],
        hour=failure_dt.hour, dow=failure_dt.weekday(), failure_code=failure_code,
        record_type="transaction", hours_since_last_attempt=hours_since(txn_row["last_action_timestamp"]),
    )

    # Relaxed cooldown (replan only): skip past outreach-type candidates if
    # cooldown is active and a silent-retry alternative exists; if the ONLY
    # candidates left are outreach and cooldown is active, block for real.
    if is_replan and cooldown_now:
        non_outreach = [c for c in scored if EXECUTION_ACTION_MAP[c["candidate_action"]] != "send_update_link"]
        if not non_outreach:
            reasoning_parts.append(f"HELD: only a customer-facing action remains and the last outreach was under {COOLDOWN_HOURS}h ago — holding.")
            return _decision(txn_row["transaction_id"], "transaction", "stop_no_action",
                              reasoning_parts, None, "cooldown_24h", attempt_number=attempt_number)
        scored = non_outreach

    best = scored[0]
    ml_action = best["candidate_action"]
    action = EXECUTION_ACTION_MAP[ml_action]

    if ml_action == "RETRY_LATER":
        retry_at = next_payday_like_date(failure_dt).isoformat(timespec="seconds")
    elif action == "retry_payment":
        retry_at = datetime.now().isoformat(timespec="seconds")
    else:
        retry_at = None

    others = ", ".join(f"{c['candidate_action']} {c['probability']*100:.0f}% (₹{c['expected_value']:,.0f})"
                        for c in scored[1:])
    replan_note = f"[Re-plan, attempt {attempt_number}] " if is_replan else ""
    reasoning_parts.append(
        f"{replan_note}ACTION: ML compared {len(scored)} candidate action(s) for {failure_code} — "
        f"chose {ml_action} (P={best['probability']*100:.0f}%, expected value ₹{best['expected_value']:,.0f})"
        + (f", over {others}" if others else "") + "."
    )


    d = _decision(txn_row["transaction_id"], "transaction", action, reasoning_parts, retry_at, None, attempt_number=attempt_number)
    d["candidate_actions"] = scored
    d["ml_selected_action"] = ml_action
    return d


def decide_receivable(inv_row, diag_row) -> dict:
    reasoning_parts = [diag_row["root_cause"]]

    gate = hard_policy_gate(inv_row, diag_row, "receivable")
    if gate:
        reasoning_parts.append(f"{'STOPPED' if gate['action']=='stop_no_action' else 'ESCALATED'}: {gate['note']}")
        return _decision(inv_row["invoice_id"], "receivable", gate["action"],
                          reasoning_parts, None, gate["stopping_rule"])

    if cooldown_active(inv_row["last_action_timestamp"]):
        reasoning_parts.append(f"HELD: last contact with this customer was under {COOLDOWN_HOURS}h ago — holding to respect the cool-off period.")
        return _decision(inv_row["invoice_id"], "receivable", "stop_no_action",
                          reasoning_parts, None, "cooldown_24h")

    days_overdue = inv_row["days_overdue"]
    candidates = candidates_for_receivable(days_overdue)
    now_dt = datetime.now()
    scored = score_candidates(
        candidates, amount=inv_row["amount"], attempt_count=inv_row["attempt_count"],
        hour=now_dt.hour, dow=now_dt.weekday(), record_type="receivable", days_overdue=days_overdue,
        hours_since_last_attempt=hours_since(inv_row["last_action_timestamp"]),
    )
    best = scored[0]
    ml_action = best["candidate_action"]
    action = EXECUTION_ACTION_MAP[ml_action]

    others = ", ".join(f"{c['candidate_action']} {c['probability']*100:.0f}% (₹{c['expected_value']:,.0f})"
                        for c in scored[1:])
    tone_note = (" This is also flagged for a human to check on in parallel, but the automated outreach is not blocked by that flag."
                 if days_overdue >= 45 else "")
    reasoning_parts.append(
        f"ACTION: ML compared {len(scored)} policy-eligible candidate(s) for a {days_overdue}-day-overdue invoice — "
        f"chose {ml_action} (P={best['probability']*100:.0f}%, expected value ₹{best['expected_value']:,.0f})"
        + (f", over {others}" if others else "") + f".{tone_note}"
    )

    d = _decision(inv_row["invoice_id"], "receivable", action, reasoning_parts, None, None)
    d["candidate_actions"] = scored
    d["ml_selected_action"] = ml_action
    return d


def decide_abandonment(ab_row, diag_row) -> dict:
    """Checkout abandonments go on the single-shot path (decision.py +
    execution.py), same as receivables and NOT the transaction re-plan loop —
    abandonment recovery is a time-decay problem (the odds only ever go down
    the longer we wait, there's no 'try again in a few minutes' upside like a
    transient bank timeout), not a retry-immediately problem. A within-run
    re-plan loop would try a second action milliseconds after the first
    failed, which does nothing for a decay-driven problem and just spends an
    extra customer contact for no real gain. Same hard policy floor
    (opt-out/cool-off) as every other record type — see hard_policy_gate."""
    reasoning_parts = [diag_row["root_cause"]]

    gate = hard_policy_gate(ab_row, diag_row, "abandonment")
    if gate:
        reasoning_parts.append(f"{'STOPPED' if gate['action']=='stop_no_action' else 'ESCALATED'}: {gate['note']}")
        return _decision(ab_row["session_id"], "abandonment", gate["action"],
                          reasoning_parts, None, gate["stopping_rule"])

    if cooldown_active(ab_row["last_action_timestamp"]):
        reasoning_parts.append(f"HELD: last contact with this customer was under {COOLDOWN_HOURS}h ago — holding to respect the cool-off period.")
        return _decision(ab_row["session_id"], "abandonment", "stop_no_action",
                          reasoning_parts, None, "cooldown_24h")

    candidates = candidates_for_abandonment(ab_row["cart_value"], ab_row["attempt_count"])
    abandoned_dt = datetime.fromisoformat(ab_row["abandoned_at"])
    now_dt = datetime.now()
    scored = score_candidates(
        candidates, amount=ab_row["cart_value"], attempt_count=ab_row["attempt_count"],
        hour=now_dt.hour, dow=now_dt.weekday(), record_type="abandonment",
        hours_since_last_attempt=hours_since(ab_row["last_action_timestamp"]),
    )
    best = scored[0]
    ml_action = best["candidate_action"]
    action = EXECUTION_ACTION_MAP[ml_action]

    others = ", ".join(f"{c['candidate_action']} {c['probability']*100:.0f}% (₹{c['expected_value']:,.0f})"
                        for c in scored[1:])
    reasoning_parts.append(
        f"ACTION: ML compared {len(scored)} policy-eligible candidate(s) — "
        f"chose {ml_action} (P={best['probability']*100:.0f}%, expected value ₹{best['expected_value']:,.0f})"
        + (f", over {others}" if others else "") + "."
    )

    d = _decision(ab_row["session_id"], "abandonment", action, reasoning_parts, None, None)
    d["candidate_actions"] = scored
    d["ml_selected_action"] = ml_action
    return d


def _decision(record_id, record_type, action, reasoning_parts, retry_at, stopping_rule, attempt_number=1):
    return {
        "record_id": record_id,
        "record_type": record_type,
        "action": action,
        "reasoning": " ".join(reasoning_parts),
        "retry_at": retry_at,
        "stopping_rule_fired": stopping_rule,
        "candidate_actions": None,
        "ml_selected_action": None,
        "attempt_number": attempt_number,
    }


def run_decisions(types=("transaction", "receivable", "abandonment")):
    conn = get_connection()
    cur = conn.cursor()
    now = datetime.now().isoformat(timespec="seconds")

    # Phase 2: operate only on the CURRENT/latest batch.
    batch_id = get_current_batch_id(conn)

    diag_by_id = {d["record_id"]: d for d in cur.execute("SELECT * FROM diagnoses WHERE batch_id = ?", (batch_id,)).fetchall()}
    decisions = []

    if "transaction" in types:
        for row in cur.execute("SELECT * FROM transactions WHERE batch_id = ?", (batch_id,)).fetchall():
            diag = diag_by_id[row["transaction_id"]]
            decisions.append(decide_transaction(row, diag))

    if "receivable" in types:
        for row in cur.execute("SELECT * FROM receivables WHERE batch_id = ?", (batch_id,)).fetchall():
            diag = diag_by_id[row["invoice_id"]]
            decisions.append(decide_receivable(row, diag))

    if "abandonment" in types:
        for row in cur.execute("SELECT * FROM checkout_abandonments WHERE batch_id = ?", (batch_id,)).fetchall():
            diag = diag_by_id[row["session_id"]]
            decisions.append(decide_abandonment(row, diag))

    # --- Hard test: no decision on a record with attempt_count >= 3 is an automated action ---
    txn_by_id = {r["transaction_id"]: r for r in cur.execute("SELECT * FROM transactions WHERE batch_id = ?", (batch_id,)).fetchall()}
    inv_by_id = {r["invoice_id"]: r for r in cur.execute("SELECT * FROM receivables WHERE batch_id = ?", (batch_id,)).fetchall()}
    ab_by_id = {r["session_id"]: r for r in cur.execute("SELECT * FROM checkout_abandonments WHERE batch_id = ?", (batch_id,)).fetchall()}

    violations = []
    for d in decisions:
        record = txn_by_id.get(d["record_id"]) or inv_by_id.get(d["record_id"]) or ab_by_id.get(d["record_id"])
        if d["action"] in AUTOMATED_ACTIONS and record["attempt_count"] >= MAX_ATTEMPTS:
            violations.append(d)
        if record["customer_opt_out"] and d["action"] != "stop_no_action":
            violations.append(d)
        diag = diag_by_id[d["record_id"]]
        if diag["risk_flag"] and d["action"] != "escalate_to_human" and not record["customer_opt_out"]:
            violations.append(d)

    assert not violations, f"STOPPING RULE VIOLATION on {len(violations)} record(s): {violations[:3]}"
    print("✅ STOPPING RULE TEST PASSED: no record exceeded max attempts, bypassed opt-out, or skipped risk escalation.\n")

    cur.execute(
        "DELETE FROM decisions WHERE batch_id = ? AND record_type IN ({})".format(",".join("?" * len(types))),
        (batch_id, *types),
    )
    cur.executemany(
        """INSERT INTO decisions
           (decision_id, record_id, record_type, attempt_number, action, reasoning, retry_at, decided_at,
            stopping_rule_fired, candidate_actions, ml_selected_action, batch_id)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        [
            (f"dec_{d['record_id']}_{d['attempt_number']}", d["record_id"], d["record_type"], d["attempt_number"],
             d["action"], d["reasoning"], d["retry_at"], now, d["stopping_rule_fired"],
             json.dumps(d["candidate_actions"]) if d["candidate_actions"] else None,
             d["ml_selected_action"], batch_id)
            for d in decisions
        ],
    )
    conn.commit()

    # --- Action distribution table ---
    print("=" * 70)
    print("ACTION DISTRIBUTION")
    print("=" * 70)
    total = len(decisions)
    counts = {}
    for d in decisions:
        counts[d["action"]] = counts.get(d["action"], 0) + 1
    for action, cnt in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"  {action:<20} count={cnt:<5} {cnt/total*100:5.1f}%")
    print(f"  {'TOTAL':<20} count={total}")

    # --- Stopping rule examples ---
    print("\n" + "=" * 70)
    print("STOPPING RULE EXAMPLES (rule fired -> automated action correctly blocked)")
    print("=" * 70)

    shown_rules = set()
    example_count = 0
    for d in decisions:
        if d["stopping_rule_fired"] and d["stopping_rule_fired"] not in shown_rules:
            shown_rules.add(d["stopping_rule_fired"])
            example_count += 1
            print(f"\n[{example_count}] Rule fired: {d['stopping_rule_fired']}")
            print(f"    Record: {d['record_type']} {d['record_id']}")
            print(f"    Action taken instead: {d['action']}")
            print(f"    Reasoning: {d['reasoning']}")
        if example_count >= 4:
            break

    rule_fired_count = sum(1 for d in decisions if d["stopping_rule_fired"])
    print(f"\nTotal decisions where a stopping rule fired: {rule_fired_count} / {total}")

    conn.close()


if __name__ == "__main__":
    run_decisions()
