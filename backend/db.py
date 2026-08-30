"""
Data-layer abstraction notes (Phase 4.2 of the production-hardening loop).

HONEST SCOPE DISCLOSURE: this module provides a SQLAlchemy engine factory
keyed off DATABASE_URL, and documents the Postgres migration path. It does
NOT rewrite the ~8 modules (schema.py, decision.py, execution.py, diagnosis.py,
baseline.py, agent_loop.py, api.py, candidate_actions.py) that currently query
via raw sqlite3 + Row-style dict access throughout. That's a genuinely large,
correctness-sensitive rewrite (every `row["col"]` access, every raw SQL
string, every `cur.executemany(...)` across the whole backend) — attempting
it inside this same hardening pass risked breaking already-verified pipeline
behavior for a change this codebase doesn't strictly need to ship the
Phase 1-3 features. Per the loop prompt's instruction #4 ("never silently
drop scope"), this is flagged explicitly rather than silently skipped or
falsely marked complete — see README's Production Readiness section and the
final summary for the same disclosure.

WHAT THIS MODULE DOES provide, today:
  - get_engine(): a SQLAlchemy Engine built from config.DATABASE_URL. Calling
    code that wants to start migrating query-by-query can use this
    immediately without any other change.
  - The connection-string-swap path: schema.py's DB_PATH / config.DATABASE_URL
    both come from config.py, so pointing at Postgres is:
        DATABASE_URL=postgresql+psycopg2://user:pass@host:5432/revenue_recovery
    in .env — no other file needs to change to make get_engine() point at
    Postgres. The remaining work is the query-layer rewrite described above,
    not the connection plumbing.

WHY SQLITE IS FINE FOR THE DEMO: single-process, single-writer batch pipeline,
no concurrent-write contention in the demo's actual usage pattern. The real
production risk that pattern exposes (double-processing on concurrent
/api/run-batch calls) is handled separately via the run-lock in
run_pipeline.py — see PIPELINE_LOCK_PATH there — not via a database-level
fix, since the failure mode is "two pipeline runs at once," not "two
overlapping SQL writes."
"""

from config import DATABASE_URL


def get_engine():
    """Returns a SQLAlchemy Engine for DATABASE_URL. Requires sqlalchemy to
    be installed (not a dependency of the core sqlite3-based pipeline today —
    add it to backend/requirements.txt when the query-layer rewrite above is
    actually undertaken)."""
    from sqlalchemy import create_engine  # imported lazily so sqlalchemy isn't a hard dependency yet
    return create_engine(DATABASE_URL)
