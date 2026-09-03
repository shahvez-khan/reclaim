"""
Regression test for the escalation operational surface (Feature Completion
Loop, Phase 4): before this, execution.py logged "Ticket logged for human
review — no automated outcome." for every escalate_to_human decision, but
no ticket object existed anywhere — a human had nothing to actually act on
beyond filtering the general records table by status.

This test runs the full pipeline against an isolated temp DB and asserts:
  1. The `escalations` table has EXACTLY one row per record whose latest
     decision is `escalate_to_human` — no more, no fewer (the exact
     consistency check named in the Phase 4 Verify block).
  2. Every escalation starts 'open'.
  3. Resolving one (schema.upsert_escalation's counterpart — simulated
     directly via the same UPDATE api.py's resolve endpoint performs) flips
     its status and sets resolved_at, without touching any other row.

Run with pytest, or directly via `python3 -m tests.test_escalations`.
"""

import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import config as _config  # noqa: E402


def _make_temp_db():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    return Path(tmp.name)


def test_escalations_table_exactly_matches_escalate_to_human_decisions():
    db_path = _make_temp_db()
    original_config_db_path = _config.DB_PATH
    _config.DB_PATH = db_path

    import schema
    original_db_path = schema.DB_PATH
    try:
        schema.DB_PATH = db_path
        schema.init_db(reset=True)

        import generate_data
        from agent_loop import run_agent_loop_for_all_transactions
        from decision import run_decisions
        from diagnosis import run_diagnosis
        from execution import run_execution

        generate_data.populate()
        schema.snapshot_initial_data()
        run_diagnosis()
        run_agent_loop_for_all_transactions()
        run_decisions(types=("receivable", "abandonment"))
        run_execution(types=("receivable", "abandonment"))

        conn = schema.get_connection()

        # --- 1. Exact match: escalations table vs. latest-decision-per-record == escalate_to_human ---
        decision_rows = conn.execute("SELECT record_id, action, attempt_number FROM decisions ORDER BY attempt_number").fetchall()
        latest_action = {}
        for r in decision_rows:
            latest_action[r["record_id"]] = r["action"]  # later attempt_number overwrites earlier
        expected_escalated = {rid for rid, action in latest_action.items() if action == "escalate_to_human"}

        escalation_rows = conn.execute("SELECT record_id, status FROM escalations").fetchall()
        actual_escalated = {r["record_id"] for r in escalation_rows}

        assert actual_escalated == expected_escalated, (
            f"escalations table doesn't exactly match escalate_to_human decisions: "
            f"{len(actual_escalated)} escalation rows vs {len(expected_escalated)} expected. "
            f"Missing: {expected_escalated - actual_escalated}. Extra: {actual_escalated - expected_escalated}."
        )
        assert len(expected_escalated) > 0, "test setup produced zero escalations — can't test anything meaningful"

        # --- 2. Every escalation starts 'open' ---
        assert all(r["status"] == "open" for r in escalation_rows)

        # --- 3. Resolving one only touches that one row ---
        target = escalation_rows[0]["record_id"]
        other_ids = [r["record_id"] for r in escalation_rows if r["record_id"] != target]

        now = datetime.now().isoformat(timespec="seconds")
        conn.execute(
            "UPDATE escalations SET status = 'resolved', resolved_at = ?, resolver_note = ? WHERE record_id = ?",
            (now, "test resolution", target),
        )
        conn.commit()

        resolved = conn.execute("SELECT status, resolved_at, resolver_note FROM escalations WHERE record_id = ?", (target,)).fetchone()
        assert resolved["status"] == "resolved"
        assert resolved["resolved_at"] == now
        assert resolved["resolver_note"] == "test resolution"

        still_open = conn.execute(
            "SELECT COUNT(*) FROM escalations WHERE record_id IN ({}) AND status != 'open'".format(
                ",".join("?" * len(other_ids))
            ),
            other_ids,
        ).fetchone()[0]
        assert still_open == 0, "resolving one escalation must not touch any other escalation's status"

        conn.close()
        print(f"PASSED: {len(expected_escalated)} escalations, exact match, resolve isolated correctly.")
    finally:
        schema.DB_PATH = original_db_path
        _config.DB_PATH = original_config_db_path
        db_path.unlink(missing_ok=True)


if __name__ == "__main__":
    import traceback
    try:
        test_escalations_table_exactly_matches_escalate_to_human_decisions()
        print("\nPASSED")
    except Exception:
        traceback.print_exc()
        print("\nFAILED")
        sys.exit(1)
