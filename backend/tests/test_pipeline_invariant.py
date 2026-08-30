"""
Integration test: runs the full pipeline (generate -> diagnose -> agent loop
-> decision/execution -> baseline) against a fixed-seed dataset in an
isolated temp DB, then asserts:
  recovered + escalated + still_failing + stopped_no_action == total
for ALL THREE record types (transactions, receivables, abandonments) — the
same consistency invariant run_pipeline.py checks at the end of every real
run, but here it's an assertion in a test rather than eyeballed console output.

Run with pytest, or directly via `python3 -m tests.test_pipeline_invariant`.
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import config as _config  # noqa: E402


def _make_temp_db():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    return Path(tmp.name)


def test_full_pipeline_invariant_holds_for_all_three_categories():
    db_path = _make_temp_db()
    _config.DB_PATH = db_path

    import schema
    # ISOLATION WARNING (see test_stopping_rule_coverage.py for the original
    # incident this pattern is meant to prevent): schema.DB_PATH is a shared
    # module-level global. Leaving it pointed at this test's temp DB after
    # the test finishes corrupts test ORDER-dependent isolation for whatever
    # test file happens to run next in the same pytest session (it will
    # silently connect to this test's now-deleted temp file instead of its
    # own). Always restore it in a finally block — never remove this.
    original_db_path = schema.DB_PATH
    original_config_db_path = _config.DB_PATH
    try:
        schema.DB_PATH = db_path
        schema.init_db(reset=True)

        import generate_data
        import random as _random
        _random.seed(42)  # fixed seed — this test is about the invariant holding, not about specific outcomes
        generate_data.populate()  # uses the project's actual configured record counts (200/400/200)

        from diagnosis import run_diagnosis
        from agent_loop import run_agent_loop_for_all_transactions
        from decision import run_decisions
        from execution import run_execution
        from schema import snapshot_initial_data, get_connection

        snapshot_initial_data()
        run_diagnosis()
        run_agent_loop_for_all_transactions()
        run_decisions(types=("receivable", "abandonment"))
        run_execution(types=("receivable", "abandonment"))

        conn = get_connection()
        cur = conn.cursor()

        txns = cur.execute("SELECT * FROM transactions").fetchall()
        recv = cur.execute("SELECT * FROM receivables").fetchall()
        aband = cur.execute("SELECT * FROM checkout_abandonments").fetchall()
        decisions = cur.execute("SELECT * FROM decisions").fetchall()

        latest_decision = {}
        for d in decisions:
            rid = d["record_id"]
            if rid not in latest_decision or d["attempt_number"] > latest_decision[rid]["attempt_number"]:
                latest_decision[rid] = d

        def record_id_of(r):
            for col in ("transaction_id", "invoice_id", "session_id"):
                if col in r.keys():
                    return r[col]

        def check_invariant(records, label):
            bucket_counts = {"recovered": 0, "escalated": 0, "still_failing": 0, "stopped_no_action": 0}
            for r in records:
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

            total = sum(bucket_counts.values())
            assert total == len(records), (
                f"[{label}] consistency check FAILED: buckets sum to {total}, expected {len(records)}. "
                f"buckets={bucket_counts}"
            )
            # every record must have received SOME decision — none silently skipped
            assert all(record_id_of(r) in latest_decision for r in records), (
                f"[{label}] at least one record has no decision recorded at all"
            )
            return bucket_counts

        txn_buckets = check_invariant(txns, "transactions")
        recv_buckets = check_invariant(recv, "receivables")
        aband_buckets = check_invariant(aband, "abandonments")

        assert len(txns) == 200
        assert len(recv) == 400
        assert len(aband) == 200

        conn.close()
        db_path.unlink(missing_ok=True)

        print(f"transactions: {txn_buckets}")
        print(f"receivables:  {recv_buckets}")
        print(f"abandonments: {aband_buckets}")
    finally:
        # CRITICAL: never leave schema.DB_PATH / config.DB_PATH pointed at
        # this test's (now-deleted) temp DB — see the ISOLATION WARNING above.
        schema.DB_PATH = original_db_path
        _config.DB_PATH = original_config_db_path


if __name__ == "__main__":
    import traceback
    try:
        test_full_pipeline_invariant_holds_for_all_three_categories()
        print("\nPASS  test_full_pipeline_invariant_holds_for_all_three_categories")
        sys.exit(0)
    except AssertionError as e:
        print(f"\nFAIL: {e}")
        sys.exit(1)
    except Exception:
        traceback.print_exc()
        sys.exit(1)
