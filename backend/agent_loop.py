"""
Phase C: bounded agentic re-plan loop (transactions only — the hero feature).

    observe -> diagnose (once) -> decide (ML+EV) -> execute -> observe result
        |                                                          |
        +-------------------- recovered? ---- yes -----------------+---> STOP
        |
        no, and attempts/candidates remain
        |
        v
    re-plan: exclude the tried action, decide again -> execute -> observe
        (repeat, capped by MAX_ATTEMPTS — this can NEVER loop forever:
         every iteration either terminates via recovery, runs out of
         un-tried candidate actions, or hits the hard attempt cap, all of
         which route to a terminal state within decide_transaction itself)

Receivables intentionally do NOT go through this loop — B2B reminder cadence
is inherently multi-day and stays on the original single-shot path in
decision.py / execution.py, per the priority scoping agreed with the user.
"""

import json
from datetime import datetime

from schema import get_connection, get_current_batch_id, upsert_escalation
from decision import decide_transaction, MAX_ATTEMPTS
from config import SAFETY_ITERATION_CAP
from execution import execute_decision

AUTOMATED_ACTIONS = {"retry_payment", "send_update_link"}
SAFETY_ITERATION_CAP = SAFETY_ITERATION_CAP  # re-exported from config.py — defensive upper bound; real termination is via MAX_ATTEMPTS/candidate exhaustion


def _row_to_dict(row):
    return {k: row[k] for k in row.keys()}


def run_agentic_transaction(txn_row, diag_row, conn, batch_id=None) -> dict:
    """Runs the full observe -> decide -> execute -> replan loop for ONE
    transaction. Persists every decision + audit event as it goes. Returns a
    small summary dict for batch-level reporting."""
    cur = conn.cursor()
    now = datetime.now().isoformat(timespec="seconds")

    txn = _row_to_dict(txn_row)
    exclude_actions = set()
    attempt_number = 1
    events = []

    for _ in range(SAFETY_ITERATION_CAP):
        is_replan = attempt_number > 1
        decision = decide_transaction(
            txn, diag_row, exclude_actions=frozenset(exclude_actions),
            is_replan=is_replan, attempt_number=attempt_number,
        )

        cur.execute(
            """INSERT INTO decisions
               (decision_id, record_id, record_type, attempt_number, action, reasoning, retry_at,
                decided_at, stopping_rule_fired, candidate_actions, ml_selected_action, batch_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (f"dec_{decision['record_id']}_{attempt_number}", decision["record_id"], "transaction",
             attempt_number, decision["action"], decision["reasoning"], decision["retry_at"], now,
             decision["stopping_rule_fired"],
             json.dumps(decision["candidate_actions"]) if decision["candidate_actions"] else None,
             decision["ml_selected_action"], batch_id),
        )

        result = execute_decision(decision, txn, now)
        is_automated = decision["action"] in AUTOMATED_ACTIONS
        recovered = result["new_status"] == "recovered"

        if recovered:
            event_type = "RECOVERY_SUCCESS"
        elif decision["action"] == "escalate_to_human":
            event_type = "ESCALATED"
            # Phase 4: give this an actual operational surface, not just a
            # status string — see schema.upsert_escalation().
            upsert_escalation(conn, decision["record_id"], "transaction",
                               decision["stopping_rule_fired"] or "escalate_to_human", batch_id, now)
        elif decision["action"] == "stop_no_action":
            event_type = "STOPPED"
        else:
            event_type = "ACTION_EXECUTED"  # automated action taken, failed, may replan

        cur.execute(
            """INSERT INTO audit_log (record_id, record_type, timestamp, event_type, action_taken, reasoning, outcome, actor, batch_id)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (decision["record_id"], "transaction", now, event_type, decision["action"],
             decision["reasoning"], result["outcome"], result["actor"], batch_id),
        )
        events.append({"attempt": attempt_number, "action": decision["action"], "outcome": result["outcome"]})

        # persist updated record state
        if is_automated:
            new_attempt_count = txn["attempt_count"] + 1
            new_last_action_ts = now if decision["action"] == "send_update_link" else txn["last_action_timestamp"]
            cur.execute(
                "UPDATE transactions SET status = ?, attempt_count = ?, last_action_timestamp = ? WHERE transaction_id = ?",
                (result["new_status"], new_attempt_count, new_last_action_ts, txn["transaction_id"]),
            )
            txn["attempt_count"] = new_attempt_count
            txn["last_action_timestamp"] = new_last_action_ts
            txn["status"] = result["new_status"]
        else:
            cur.execute(
                "UPDATE transactions SET status = ? WHERE transaction_id = ?",
                (result["new_status"], txn["transaction_id"]),
            )
            txn["status"] = result["new_status"]

        if recovered or not is_automated:
            break  # terminal: recovered, escalated, or stopped

        # failed automated attempt — replan for the next iteration
        if decision["ml_selected_action"]:
            exclude_actions.add(decision["ml_selected_action"])
        cur.execute(
            """INSERT INTO audit_log (record_id, record_type, timestamp, event_type, action_taken, reasoning, outcome, actor, batch_id)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (decision["record_id"], "transaction", now, "REPLANNED", decision["action"],
             f"{decision['action']} did not recover the payment — re-diagnosing and selecting the next best candidate action.",
             "Replanning", "agent", batch_id),
        )
        attempt_number += 1

    return {"transaction_id": txn["transaction_id"], "final_status": txn["status"], "attempts": attempt_number, "events": events}


def run_agent_loop_for_all_transactions():
    conn = get_connection()
    cur = conn.cursor()

    # Phase 2: operate only on the CURRENT/latest batch's transactions — old
    # batches' decisions/audit_log rows must persist untouched.
    batch_id = get_current_batch_id(conn)

    diag_by_id = {d["record_id"]: d for d in cur.execute("SELECT * FROM diagnoses WHERE batch_id = ?", (batch_id,)).fetchall()}
    cur.execute("DELETE FROM decisions WHERE record_type = 'transaction' AND batch_id = ?", (batch_id,))
    cur.execute("DELETE FROM audit_log WHERE record_type = 'transaction' AND batch_id = ?", (batch_id,))

    txn_rows = cur.execute("SELECT * FROM transactions WHERE batch_id = ?", (batch_id,)).fetchall()
    results = []
    for row in txn_rows:
        diag = diag_by_id[row["transaction_id"]]
        results.append(run_agentic_transaction(row, diag, conn, batch_id=batch_id))

    conn.commit()

    replanned = sum(1 for r in results if r["attempts"] > 1)
    recovered = sum(1 for r in results if r["final_status"] == "recovered")
    print(f"Agent loop processed {len(results)} transactions")
    print(f"  Re-planned (tried a 2nd+ action after a failure): {replanned}")
    print(f"  Recovered: {recovered}")
    print(f"  Max attempts seen: {max(r['attempts'] for r in results)}")

    conn.close()
    return results


if __name__ == "__main__":
    run_agent_loop_for_all_transactions()
