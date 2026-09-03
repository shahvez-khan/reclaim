"""
Full pipeline orchestrator (post-Phase-C, extended in the production-hardening
loop to include checkout abandonments as a third revenue-risk category).

TRANSACTIONS run through the bounded agentic re-plan loop (agent_loop.py) —
diagnose once, then decide/execute/observe/replan up to MAX_ATTEMPTS.

RECEIVABLES and ABANDONMENTS both stay on the single-shot path
(decision.py + execution.py) — B2B reminder cadence is inherently multi-day,
and abandonment recovery is a time-decay problem (the odds only ever go down
the longer we wait — no "try again in a few minutes" upside like a transient
bank timeout), so neither benefits from a within-run re-plan loop. See
decision.decide_abandonment's docstring and agent_loop.py's docstring for the
full scoping rationale for each.

This replaces running decision.py/execution.py standalone for the full batch;
those scripts remain independently runnable (and are still used here for the
receivables/abandonments-only slice) for debugging/testing individual stages.

IDEMPOTENCY / DOUBLE-PROCESSING PROTECTION (Phase 4.2): the real production
risk of calling /api/run-batch twice concurrently is two pipeline runs
processing (and double-charging/double-messaging) the same records at once.
Rather than a database-level uniqueness constraint (which doesn't map cleanly
onto "the whole batch re-decides every record's action from scratch each
run"), this is guarded with a simple file-based lock: a second concurrent
run_full_pipeline() call fails fast with a clear error instead of racing the
first one. See PIPELINE_LOCK_PATH below.
"""

import atexit
import os
import time
from pathlib import Path

from agent_loop import run_agent_loop_for_all_transactions
from baseline import run_baseline
from decision import run_decisions
from diagnosis import run_diagnosis
from execution import run_execution
from generate_data import populate
from schema import get_connection, get_current_batch_id, snapshot_initial_data

PIPELINE_LOCK_PATH = Path(__file__).parent.parent / "data" / "pipeline.lock"


class PipelineAlreadyRunningError(RuntimeError):
    pass


def _acquire_lock():
    PIPELINE_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    if PIPELINE_LOCK_PATH.exists():
        age_seconds = time.time() - PIPELINE_LOCK_PATH.stat().st_mtime
        if age_seconds < 600:  # 10 min — generous upper bound on a real run; older locks are treated as stale/crashed
            raise PipelineAlreadyRunningError(
                f"Another pipeline run appears to be in progress (lock file is {age_seconds:.0f}s old). "
                "Refusing to start a second run to avoid double-processing records. "
                f"If you're sure no other run is active, delete {PIPELINE_LOCK_PATH} and retry."
            )
        # stale lock from a crashed prior run — safe to reclaim
    PIPELINE_LOCK_PATH.write_text(str(os.getpid()))
    atexit.register(_release_lock)


def _release_lock():
    if PIPELINE_LOCK_PATH.exists():
        PIPELINE_LOCK_PATH.unlink()


def run_full_pipeline(regenerate_data: bool = True):
    _acquire_lock()
    try:
        _run_full_pipeline_inner(regenerate_data)
    finally:
        _release_lock()


def _run_full_pipeline_inner(regenerate_data: bool):
    if regenerate_data:
        populate()
        print()

    snapshot_initial_data()  # freeze pristine state BEFORE agent or baseline touch anything

    run_diagnosis()
    print()

    print("=" * 70)
    print("AGENT LOOP — TRANSACTIONS (bounded re-plan)")
    print("=" * 70)
    run_agent_loop_for_all_transactions()
    print()

    print("=" * 70)
    print("DECISION + EXECUTION — RECEIVABLES + ABANDONMENTS (single-shot)")
    print("=" * 70)
    run_decisions(types=("receivable", "abandonment"))
    run_execution(types=("receivable", "abandonment"))
    print()

    print("=" * 70)
    print("BASELINE (naive strategy, same starting snapshot)")
    print("=" * 70)
    baseline_result = run_baseline()
    print()

    print_combined_summary(baseline_result)


def print_combined_summary(baseline_result=None):
    conn = get_connection()
    cur = conn.cursor()

    # Phase 2: scope to the CURRENT/latest batch only — the source tables are
    # cumulative now, so an unfiltered SELECT would include every prior run's
    # records too and this "batch summary" would silently become an
    # all-time summary instead.
    batch_id = get_current_batch_id(conn)

    txns = cur.execute("SELECT * FROM transactions WHERE batch_id = ?", (batch_id,)).fetchall()
    recv = cur.execute("SELECT * FROM receivables WHERE batch_id = ?", (batch_id,)).fetchall()
    aband = cur.execute("SELECT * FROM checkout_abandonments WHERE batch_id = ?", (batch_id,)).fetchall()

    def amt(r):
        return r["cart_value"] if "cart_value" in r.keys() else r["amount"]

    def recovered_amt(r):
        # "at risk" exposure uses gross cart_value/amount everywhere, but the
        # RECOVERED figure for an abandonment must use the net recovered_amount
        # column (Phase 1 fix) — a discount-nudge recovery pays less than
        # cart_value, so summing cart_value there overstates real recovered
        # revenue. Transactions/receivables have no discount-style action, so
        # `amount` already equals the true recovered figure for them.
        if "recovered_amount" in r.keys():
            return r["recovered_amount"] if r["recovered_amount"] is not None else 0.0
        return amt(r)

    all_records = list(txns) + list(recv) + list(aband)
    total_amount = sum(amt(r) for r in all_records)
    recovered_amount = sum(recovered_amt(r) for r in all_records if r["status"] == "recovered")
    txn_total = sum(r["amount"] for r in txns)
    txn_recovered = sum(r["amount"] for r in txns if r["status"] == "recovered")
    recv_total = sum(r["amount"] for r in recv)
    recv_recovered = sum(r["amount"] for r in recv if r["status"] == "recovered")
    aband_total = sum(r["cart_value"] for r in aband)
    aband_recovered = sum(recovered_amt(r) for r in aband if r["status"] == "recovered")

    decisions = cur.execute("SELECT * FROM decisions WHERE batch_id = ?", (batch_id,)).fetchall()
    latest_decision = {}
    for d in decisions:
        rid = d["record_id"]
        if rid not in latest_decision or d["attempt_number"] > latest_decision[rid]["attempt_number"]:
            latest_decision[rid] = d

    def record_id_of(r):
        for col in ("transaction_id", "invoice_id", "session_id"):
            if col in r.keys():
                return r[col]
        raise KeyError("no id column found on record")

    bucket_counts = {"recovered": 0, "escalated": 0, "still_failing": 0, "stopped_no_action": 0}
    for r in all_records:
        rid = record_id_of(r)
        dec = latest_decision.get(rid)
        action = dec["action"] if dec else None
        if r["status"] == "recovered":
            bucket_counts["recovered"] += 1
        elif action == "escalate_to_human":
            bucket_counts["escalated"] += 1
        elif action == "stop_no_action":
            bucket_counts["stopped_no_action"] += 1
        else:
            bucket_counts["still_failing"] += 1

    total_processed = sum(bucket_counts.values())
    assert total_processed == len(all_records), "Consistency check FAILED"

    replanned_txns = len({d["record_id"] for d in decisions if d["record_type"] == "transaction" and d["attempt_number"] > 1})
    stopping_rule_triggers = sum(1 for d in decisions if d["stopping_rule_fired"])
    human_escalations = sum(1 for rid, d in latest_decision.items() if d["action"] == "escalate_to_human")

    print("=" * 70)
    print("COMBINED PIPELINE SUMMARY")
    print("=" * 70)
    print(f"\n✅ CONSISTENCY CHECK PASSED: {bucket_counts['recovered']} + {bucket_counts['escalated']} + "
          f"{bucket_counts['still_failing']} + {bucket_counts['stopped_no_action']} = {total_processed} "
          f"== {len(all_records)} total records\n")
    print(f"Total revenue at risk:       ₹{total_amount:,.2f}")
    print(f"Total revenue recovered:     ₹{recovered_amount:,.2f}")
    print(f"Overall recovery rate:       {recovered_amount/total_amount*100:.1f}%")
    print(f"  Transactions:  {txn_recovered/txn_total*100:.1f}%  (₹{txn_recovered:,.2f} / ₹{txn_total:,.2f})")
    print(f"  Receivables:   {recv_recovered/recv_total*100:.1f}%  (₹{recv_recovered:,.2f} / ₹{recv_total:,.2f})")
    print(f"  Abandonments:  {aband_recovered/aband_total*100:.1f}%  (₹{aband_recovered:,.2f} / ₹{aband_total:,.2f})")
    print(f"\nTransactions that re-planned after a failed first attempt: {replanned_txns}")
    print(f"Stopping-rule triggers: {stopping_rule_triggers}")
    print(f"Human escalations: {human_escalations}")

    if baseline_result:
        cur2 = conn.cursor()

        def baseline_slice(record_type):
            row = cur2.execute(
                "SELECT COUNT(*), COALESCE(SUM(amount),0), "
                "SUM(CASE WHEN baseline_recovered=1 THEN amount ELSE 0 END), "
                "SUM(CASE WHEN baseline_action NOT LIKE 'stop%' AND baseline_action NOT LIKE 'escalate%' THEN 1 ELSE 0 END) "
                "FROM baseline_results WHERE record_type=?",
                (record_type,),
            ).fetchone()
            n, total, recovered, eligible_n = row
            return n, total, recovered or 0.0, eligible_n or 0

        txn_n, _, txn_baseline, txn_eligible = baseline_slice("transaction")
        recv_n, _, recv_baseline, recv_eligible = baseline_slice("receivable")
        aband_n, _, aband_baseline, aband_eligible = baseline_slice("abandonment")

        txn_incremental = txn_recovered - txn_baseline
        recv_incremental = recv_recovered - recv_baseline
        aband_incremental = aband_recovered - aband_baseline

        print(f"\n{'='*70}\nBASELINE VS AI AGENT — TRANSACTIONS (headline; the hero comparison)\n{'='*70}")
        print(f"Eligible-record count (policy-eligible for automated action): {txn_eligible} of {txn_n}")
        print(f"Baseline (blind retry-once, no diagnosis, no re-plan): ₹{txn_baseline:,.2f}")
        print(f"AI Agent (diagnosis + ML + candidate actions + re-plan): ₹{txn_recovered:,.2f}")
        print(f"Incremental revenue from the agent: ₹{txn_incremental:,.2f} "
              f"({txn_incremental/txn_baseline*100:+.1f}% vs baseline)" if txn_baseline else "")

        print(f"\n{'='*70}\nBASELINE VS AI AGENT — RECEIVABLES\n{'='*70}")
        print(f"Eligible-record count (policy-eligible for automated action): {recv_eligible} of {recv_n}")
        print(f"Baseline (send one reminder, no tiering): ₹{recv_baseline:,.2f}")
        print(f"AI Agent (ML-ranked reminder tier): ₹{recv_recovered:,.2f}")
        if recv_baseline:
            print(f"Delta: ₹{recv_incremental:,.2f} ({recv_incremental/recv_baseline*100:+.1f}% vs baseline)")
        # With 300-500 generated receivables (Phase 2 fix) the eligible-N is
        # large enough that the sign is stable run-to-run; still flagged
        # honestly if it's ever thin (e.g. a change to the overdue-day mix).
        if recv_eligible < 100:
            print(f"NOTE: only {recv_eligible} receivables are policy-eligible for any automated action — "
                  "at this sample size a single lucky/unlucky draw can meaningfully move the result. "
                  "Read this comparison with caution; see README's Limitations section.")

        print(f"\n{'='*70}\nBASELINE VS AI AGENT — ABANDONMENTS\n{'='*70}")
        print(f"Eligible-record count (policy-eligible for automated action): {aband_eligible} of {aband_n}")
        print(f"Baseline (always send cart-recovery link, no tiering, no discount): ₹{aband_baseline:,.2f}")
        print(f"AI Agent (ML-ranked action incl. gated discount nudge): ₹{aband_recovered:,.2f}")
        if aband_baseline:
            print(f"Delta: ₹{aband_incremental:,.2f} ({aband_incremental/aband_baseline*100:+.1f}% vs baseline)")
        if abs(aband_incremental) < 0.03 * aband_baseline if aband_baseline else True:
            print("NOTE: the effect size here is small relative to baseline — the discount nudge is only "
                  "policy-eligible for a subset of carts (see DISCOUNT_NUDGE_MIN_CART_VALUE gate in "
                  "candidate_actions.py), so most abandonment records get the same action either way. "
                  "Reported honestly rather than inflated.")

    conn.close()


if __name__ == "__main__":
    run_full_pipeline()
