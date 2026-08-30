"""
Loop 5: API layer for the dashboard.

Serves:
  GET  /api/summary                -> KPIs, bucket counts, failure_code breakdown, stopping-rule counts
  GET  /api/records?status=&type=  -> filterable list of transactions/receivables/abandonments with decision/outcome
  GET  /api/audit/{record_id}      -> full chain: diagnosis -> decision -> execution/audit log
  POST /api/run-batch              -> re-runs the whole pipeline fresh (rate-limited — see below)
  GET  /api/health                 -> liveness + DB connectivity check, for a load balancer/uptime monitor
  GET  /                           -> static dashboard

PRODUCTION-HARDENING NOTES (Phase 4.3/4.4 of the hardening loop):
  - All query/path params are validated via Pydantic-backed FastAPI typing;
    malformed input gets a proper 4xx, not a 500 from a downstream KeyError.
  - Every unhandled exception is caught by the global handler below and
    returned as a consistent {"error": {...}} JSON shape, never a raw
    traceback to the client.
  - /api/run-batch is rate-limited (in-memory token bucket per-process) —
    explicitly a demo-appropriate stand-in for real auth/rate-limiting
    middleware (e.g. an API gateway or Redis-backed limiter in front of a
    real deployment), not a claim that this is production-grade throttling.
  - Structured JSON logging with a run_id ties every log line for one batch
    run together — see logging_config.py.
"""

import logging
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from schema import get_connection
from logging_config import configure_logging
from config import RUN_BATCH_RATE_LIMIT_MAX, RUN_BATCH_RATE_LIMIT_WINDOW_SECONDS

configure_logging()
logger = logging.getLogger("revenue_recovery.api")

BACKEND_DIR = Path(__file__).parent
FRONTEND_DIR = BACKEND_DIR.parent / "frontend"

app = FastAPI(title="Reclaim API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

AUTOMATED_ACTIONS = {
    "retry_payment", "send_update_link", "send_reminder", "escalate_reminder",
    "send_cart_recovery_link", "send_discount_nudge",
}
VALID_RECORD_TYPES = {"transaction", "receivable", "abandonment"}
VALID_STATUSES = {"recovered", "escalated", "still_failing", "stopped_no_action"}


# --------------------------------------------------------------------------
# Structured error handling (Phase 4.3): every error the client sees has the
# same JSON shape, whether it's a validation failure, a 404, or something
# unexpected blowing up deep in the DB layer.
# --------------------------------------------------------------------------

def _error_body(status_code: int, message: str, detail: str | None = None) -> dict:
    return {"error": {"status": status_code, "message": message, "detail": detail}}


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content=_error_body(exc.status_code, str(exc.detail)))


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("unhandled_exception", extra={"path": str(request.url)})
    return JSONResponse(status_code=500, content=_error_body(500, "internal_server_error"))


# --------------------------------------------------------------------------
# Rate limiting (Phase 4.3): a minimal in-memory fixed-window limiter on the
# one endpoint that triggers real compute. This is a demo-appropriate stand-in
# — a real deployment would put this behind an API gateway or a shared
# (Redis-backed) limiter so it works correctly across multiple processes.
# --------------------------------------------------------------------------

_RUN_BATCH_WINDOW_SECONDS = RUN_BATCH_RATE_LIMIT_WINDOW_SECONDS
_RUN_BATCH_MAX_PER_WINDOW = RUN_BATCH_RATE_LIMIT_MAX
_run_batch_calls: list[float] = []


def _rate_limit_run_batch():
    now = time.time()
    cutoff = now - _RUN_BATCH_WINDOW_SECONDS
    while _run_batch_calls and _run_batch_calls[0] < cutoff:
        _run_batch_calls.pop(0)
    if len(_run_batch_calls) >= _RUN_BATCH_MAX_PER_WINDOW:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded: max {_RUN_BATCH_MAX_PER_WINDOW} batch runs per "
                    f"{_RUN_BATCH_WINDOW_SECONDS}s. Try again shortly.",
        )
    _run_batch_calls.append(now)


def _latest_decisions(cur):
    """Multiple decisions can now exist per record (agent re-plan attempts) —
    always use the LATEST one (highest attempt_number) as the record's
    current/final decision for bucketing and summary purposes."""
    latest = {}
    for d in cur.execute("SELECT * FROM decisions ORDER BY attempt_number").fetchall():
        latest[d["record_id"]] = d  # later attempt_number overwrites earlier, since ordered ascending
    return latest


def _bucket_for(record_status, dec):
    if record_status == "recovered":
        return "recovered"
    if dec and dec["action"] == "escalate_to_human":
        return "escalated"
    if dec and dec["action"] == "stop_no_action":
        return "stopped_no_action"
    return "still_failing"


@app.get("/api/health")
def health():
    """Liveness + DB connectivity check — standard for anything sitting
    behind a load balancer or uptime monitor. Returns 200 only if the DB is
    actually reachable and queryable, not just that the process is up."""
    try:
        conn = get_connection()
        conn.execute("SELECT 1").fetchone()
        conn.close()
        return {"status": "ok", "db": "reachable"}
    except Exception as e:
        logger.error("health_check_failed", extra={"error": str(e)})
        raise HTTPException(status_code=503, detail="database unreachable")


@app.get("/api/summary")
def get_summary():
    conn = get_connection()
    cur = conn.cursor()

    txns = cur.execute("SELECT * FROM transactions").fetchall()
    recv = cur.execute("SELECT * FROM receivables").fetchall()
    aband = cur.execute("SELECT * FROM checkout_abandonments").fetchall()
    decisions = _latest_decisions(cur)
    diagnoses = {d["record_id"]: d for d in cur.execute("SELECT * FROM diagnoses").fetchall()}

    def amt(r):
        return r["cart_value"] if "cart_value" in r.keys() else r["amount"]

    def recovered_amt(r):
        # See run_pipeline.py::print_combined_summary's recovered_amt for the
        # same reasoning: abandonments must use the net recovered_amount
        # column (Phase 1 discount-accounting fix), not gross cart_value.
        if "recovered_amount" in r.keys():
            return r["recovered_amount"] if r["recovered_amount"] is not None else 0.0
        return amt(r)

    def record_id_of(r):
        for col in ("transaction_id", "invoice_id", "session_id"):
            if col in r.keys():
                return r[col]

    all_records = list(txns) + list(recv) + list(aband)
    total_amount = sum(amt(r) for r in all_records)
    recovered_amount = sum(recovered_amt(r) for r in all_records if r["status"] == "recovered")

    txn_total = sum(r["amount"] for r in txns)
    txn_recovered = sum(r["amount"] for r in txns if r["status"] == "recovered")
    recv_total = sum(r["amount"] for r in recv)
    recv_recovered = sum(r["amount"] for r in recv if r["status"] == "recovered")
    aband_total = sum(r["cart_value"] for r in aband)
    aband_recovered = sum(recovered_amt(r) for r in aband if r["status"] == "recovered")

    bucket_counts = {"recovered": 0, "escalated": 0, "still_failing": 0, "stopped_no_action": 0}
    for r in all_records:
        rid = record_id_of(r)
        dec = decisions.get(rid)
        action = dec["action"] if dec else None
        if r["status"] == "recovered":
            bucket_counts["recovered"] += 1
        elif action == "escalate_to_human":
            bucket_counts["escalated"] += 1
        elif action == "stop_no_action":
            bucket_counts["stopped_no_action"] += 1
        else:
            bucket_counts["still_failing"] += 1

    stopping_rule_triggers = sum(1 for d in decisions.values() if d["stopping_rule_fired"])
    human_escalations = sum(1 for d in decisions.values() if d["action"] == "escalate_to_human")

    by_failure_code = {}
    for r in txns:
        fc = r["failure_code"]
        by_failure_code.setdefault(fc, {"total": 0, "at_risk": 0.0, "recovered": 0, "recovered_amt": 0.0})
        by_failure_code[fc]["total"] += 1
        by_failure_code[fc]["at_risk"] += r["amount"]
        if r["status"] == "recovered":
            by_failure_code[fc]["recovered"] += 1
            by_failure_code[fc]["recovered_amt"] += r["amount"]

    stopping_rule_breakdown = {}
    for d in decisions.values():
        if d["stopping_rule_fired"]:
            stopping_rule_breakdown[d["stopping_rule_fired"]] = stopping_rule_breakdown.get(d["stopping_rule_fired"], 0) + 1

    manual_followup_count = sum(1 for d in diagnoses.values() if d["needs_manual_followup"])
    replanned_count = len({d["record_id"] for d in cur.execute(
        "SELECT record_id FROM decisions WHERE attempt_number > 1").fetchall()})

    conn.close()

    return {
        "total_records": len(all_records),
        "total_at_risk": total_amount,
        "total_recovered": recovered_amount,
        "recovery_rate_blended": round(recovered_amount / total_amount * 100, 1) if total_amount else 0,
        "recovery_rate_transactions": round(txn_recovered / txn_total * 100, 1) if txn_total else 0,
        "recovery_rate_receivables": round(recv_recovered / recv_total * 100, 1) if recv_total else 0,
        "recovery_rate_abandonments": round(aband_recovered / aband_total * 100, 1) if aband_total else 0,
        "txn_total": txn_total,
        "txn_recovered": txn_recovered,
        "recv_total": recv_total,
        "recv_recovered": recv_recovered,
        "aband_total": aband_total,
        "aband_recovered": aband_recovered,
        "bucket_counts": bucket_counts,
        "stopping_rule_triggers": stopping_rule_triggers,
        "human_escalations": human_escalations,
        "manual_followup_count": manual_followup_count,
        "replanned_count": replanned_count,
        "by_failure_code": by_failure_code,
        "stopping_rule_breakdown": stopping_rule_breakdown,
    }


@app.get("/api/records")
def get_records(status: str | None = None, record_type: str | None = None):
    # Request validation (Phase 4.3): reject malformed filter values with a
    # proper 400 instead of silently returning an empty/wrong result set.
    if status is not None and status not in VALID_STATUSES:
        raise HTTPException(status_code=400, detail=f"invalid status filter: {status!r}. Must be one of {sorted(VALID_STATUSES)}")
    if record_type is not None and record_type not in VALID_RECORD_TYPES:
        raise HTTPException(status_code=400, detail=f"invalid record_type filter: {record_type!r}. Must be one of {sorted(VALID_RECORD_TYPES)}")

    conn = get_connection()
    cur = conn.cursor()
    decisions = _latest_decisions(cur)
    diagnoses = {d["record_id"]: d for d in cur.execute("SELECT * FROM diagnoses").fetchall()}

    results = []

    if record_type is None or record_type == "transaction":
        for r in cur.execute("SELECT * FROM transactions").fetchall():
            dec = decisions.get(r["transaction_id"])
            diag = diagnoses.get(r["transaction_id"])
            bucket = _bucket_for(r["status"], dec)
            if status and status != bucket:
                continue
            results.append({
                "record_id": r["transaction_id"],
                "record_type": "transaction",
                "customer_id": r["customer_id"],
                "amount": r["amount"],
                "detail": r["failure_code"],
                "payment_method": r["payment_method"],
                "status": r["status"],
                "bucket": bucket,
                "action": dec["action"] if dec else None,
                "risk_flag": bool(diag["risk_flag"]) if diag else False,
                "needs_manual_followup": bool(diag["needs_manual_followup"]) if diag else False,
                "attempt_count": r["attempt_count"],
                "replanned": bool(dec and dec["attempt_number"] > 1),
            })

    if record_type is None or record_type == "receivable":
        for r in cur.execute("SELECT * FROM receivables").fetchall():
            dec = decisions.get(r["invoice_id"])
            diag = diagnoses.get(r["invoice_id"])
            bucket = _bucket_for(r["status"], dec)
            if status and status != bucket:
                continue
            results.append({
                "record_id": r["invoice_id"],
                "record_type": "receivable",
                "customer_id": r["customer_id"],
                "amount": r["amount"],
                "detail": f"{r['days_overdue']} days overdue",
                "payment_method": None,
                "status": r["status"],
                "bucket": bucket,
                "action": dec["action"] if dec else None,
                "risk_flag": bool(diag["risk_flag"]) if diag else False,
                "needs_manual_followup": bool(diag["needs_manual_followup"]) if diag else False,
                "attempt_count": r["attempt_count"],
                "replanned": bool(dec and dec["attempt_number"] > 1),
            })

    if record_type is None or record_type == "abandonment":
        for r in cur.execute("SELECT * FROM checkout_abandonments").fetchall():
            dec = decisions.get(r["session_id"])
            diag = diagnoses.get(r["session_id"])
            bucket = _bucket_for(r["status"], dec)
            if status and status != bucket:
                continue
            minutes_ago = (datetime.now() - datetime.fromisoformat(r["abandoned_at"])).total_seconds() / 60
            recency_tier = "warm" if minutes_ago < 60 else "cooling" if minutes_ago < 24 * 60 else "cold"
            results.append({
                "record_id": r["session_id"],
                "record_type": "abandonment",
                "customer_id": r["customer_id"],
                "amount": r["cart_value"],
                "detail": f"cart abandoned ({recency_tier})" + (" · payment attempted" if r["payment_attempted"] else ""),
                "payment_method": None,
                "status": r["status"],
                "bucket": bucket,
                "action": dec["action"] if dec else None,
                "risk_flag": bool(diag["risk_flag"]) if diag else False,
                "needs_manual_followup": bool(diag["needs_manual_followup"]) if diag else False,
                "attempt_count": r["attempt_count"],
                "replanned": bool(dec and dec["attempt_number"] > 1),
            })

    conn.close()
    results.sort(key=lambda x: -x["amount"])
    return results


@app.get("/api/audit/{record_id}")
def get_audit(record_id: str):
    if not record_id or len(record_id) > 128:
        raise HTTPException(status_code=400, detail="invalid record_id")

    conn = get_connection()
    cur = conn.cursor()

    diag = cur.execute("SELECT * FROM diagnoses WHERE record_id = ?", (record_id,)).fetchone()
    decs = cur.execute("SELECT * FROM decisions WHERE record_id = ? ORDER BY attempt_number", (record_id,)).fetchall()
    log = cur.execute("SELECT * FROM audit_log WHERE record_id = ? ORDER BY log_id", (record_id,)).fetchall()

    record = cur.execute("SELECT * FROM transactions WHERE transaction_id = ?", (record_id,)).fetchone()
    record_type = "transaction"
    if record is None:
        record = cur.execute("SELECT * FROM receivables WHERE invoice_id = ?", (record_id,)).fetchone()
        record_type = "receivable"
    if record is None:
        record = cur.execute("SELECT * FROM checkout_abandonments WHERE session_id = ?", (record_id,)).fetchone()
        record_type = "abandonment"

    conn.close()

    if record is None:
        raise HTTPException(status_code=404, detail="Record not found")

    record_dict = dict(record)
    if record_type == "abandonment":
        record_dict["amount"] = record_dict["cart_value"]  # normalize for the shared frontend receipt renderer

    return {
        "record_id": record_id,
        "record_type": record_type,
        "record": record_dict,
        "diagnosis": dict(diag) if diag else None,
        "decisions": [dict(d) for d in decs],  # every attempt, in order — [-1] is the latest/final one
        "audit_log": [dict(row) for row in log],
    }


class RunBatchResponse(BaseModel):
    status: str
    message: str
    run_id: str


@app.post("/api/run-batch", response_model=RunBatchResponse)
def run_batch():
    """Re-runs the entire pipeline fresh: data -> diagnosis -> agent loop
    (transactions) -> decision+execution (receivables + abandonments)."""
    _rate_limit_run_batch()

    run_id = uuid.uuid4().hex[:12]
    logger.info("run_batch_started", extra={"run_id": run_id})

    result = subprocess.run(
        [sys.executable, str(BACKEND_DIR / "run_pipeline.py")],
        capture_output=True, text=True, cwd=BACKEND_DIR,
    )
    if result.returncode != 0:
        if "PipelineAlreadyRunningError" in result.stderr:
            logger.warning("run_batch_conflict", extra={"run_id": run_id})
            raise HTTPException(status_code=409, detail="A pipeline run is already in progress. Try again shortly.")
        logger.error("run_batch_failed", extra={"run_id": run_id, "returncode": result.returncode})
        raise HTTPException(status_code=500, detail=f"run_pipeline.py failed: {result.stderr[-2000:]}")

    logger.info("run_batch_completed", extra={"run_id": run_id})
    return RunBatchResponse(status="ok", message="Batch re-run complete.", run_id=run_id)


@app.get("/api/baseline")
def get_baseline_comparison():
    conn = get_connection()
    cur = conn.cursor()

    def slice_for(record_type: str, eligible_threshold: int = 100):
        row = cur.execute(
            "SELECT COUNT(*), COALESCE(SUM(amount),0), "
            "SUM(CASE WHEN baseline_action NOT LIKE 'stop%' AND baseline_action NOT LIKE 'escalate%' THEN 1 ELSE 0 END) "
            "FROM baseline_results WHERE record_type=?",
            (record_type,),
        ).fetchone()
        n, _, eligible_n = row
        baseline_recovered = cur.execute(
            "SELECT COALESCE(SUM(amount),0) FROM baseline_results WHERE record_type=? AND baseline_recovered=1",
            (record_type,),
        ).fetchone()[0]
        return n, eligible_n or 0, baseline_recovered

    txn_n, txn_eligible, txn_baseline = slice_for("transaction")
    recv_n, recv_eligible, recv_baseline = slice_for("receivable")
    aband_n, aband_eligible, aband_baseline = slice_for("abandonment")

    txn_agent = cur.execute("SELECT COALESCE(SUM(amount),0) FROM transactions WHERE status='recovered'").fetchone()[0]
    recv_agent = cur.execute("SELECT COALESCE(SUM(amount),0) FROM receivables WHERE status='recovered'").fetchone()[0]
    # recovered_amount (not cart_value) — a discount-nudge recovery pays less
    # than cart_value; see Phase 1 discount-accounting fix.
    aband_agent = cur.execute("SELECT COALESCE(SUM(recovered_amount),0) FROM checkout_abandonments WHERE status='recovered'").fetchone()[0]

    conn.close()

    def compare(n, eligible, baseline, agent, small_sample_note=None):
        d = {
            "n_records": n,
            "eligible_records": eligible,
            "baseline_recovered": baseline,
            "agent_recovered": agent,
            "incremental": agent - baseline,
            "incremental_pct": round((agent - baseline) / baseline * 100, 1) if baseline else None,
        }
        if small_sample_note and eligible < 100:
            d["caveat"] = small_sample_note
        return d

    return {
        "transactions": compare(txn_n, txn_eligible, txn_baseline, txn_agent),
        "receivables": compare(
            recv_n, recv_eligible, recv_baseline, recv_agent,
            small_sample_note="Eligible-record count is below 100 — at this sample size a single "
                                "lucky/unlucky draw can meaningfully move the result. Read with caution.",
        ),
        "abandonments": compare(
            aband_n, aband_eligible, aband_baseline, aband_agent,
            small_sample_note="Eligible-record count is below 100 — at this sample size a single "
                                "lucky/unlucky draw can meaningfully move the result. Read with caution.",
        ),
    }


@app.get("/api/hero-examples")
def get_hero_examples():
    """Three curated, always-fresh examples for a live walkthrough — re-derived
    from whatever the current batch run actually produced, so these never go
    stale after a re-run."""
    conn = get_connection()
    cur = conn.cursor()

    retry_success = cur.execute("""
        SELECT d.record_id FROM decisions d
        WHERE d.record_type='transaction' AND d.attempt_number >= 2
        AND EXISTS (SELECT 1 FROM audit_log a WHERE a.record_id=d.record_id AND a.event_type='RECOVERY_SUCCESS')
        LIMIT 1
    """).fetchone()

    risk_escalated = cur.execute("""
        SELECT record_id FROM decisions WHERE stopping_rule_fired='risk_flag' LIMIT 1
    """).fetchone()

    max_attempts = cur.execute("""
        SELECT record_id FROM decisions WHERE stopping_rule_fired='max_attempts' AND attempt_number >= 3 LIMIT 1
    """).fetchone()
    if not max_attempts:
        max_attempts = cur.execute("SELECT record_id FROM decisions WHERE stopping_rule_fired='max_attempts' LIMIT 1").fetchone()

    conn.close()

    examples = []
    if retry_success:
        examples.append({"record_id": retry_success["record_id"], "label": "Re-plan then recover",
                          "blurb": "First action failed, agent tried a different one, it worked."})
    if risk_escalated:
        examples.append({"record_id": risk_escalated["record_id"], "label": "Risk case escalated",
                          "blurb": "Flagged for fraud risk — routed to a human, never auto-retried."})
    if max_attempts:
        examples.append({"record_id": max_attempts["record_id"], "label": "Stopped at max attempts",
                          "blurb": "3 automated tries, no luck — escalated instead of looping forever."})
    return examples


app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(FRONTEND_DIR / "index.html")
