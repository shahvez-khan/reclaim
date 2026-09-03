"""
Simple versioned migration runner (Phase 4.2 of the production-hardening
loop) — an alternative to schema.init_db(reset=True) for evolving the
schema without wiping data.

migrations/0001_initial.sql is the schema as of this hardening pass
(transactions, receivables, checkout_abandonments, diagnoses, decisions,
audit_log, snapshots, baseline_results). Future schema changes should be
added as migrations/000N_description.sql rather than edited into
schema.SCHEMA_SQL directly, so existing databases can be upgraded in place.

This is intentionally simple (no rollback support, no dependency on a
migration framework like Alembic) — sufficient for a project of this size,
and it's explicitly documented as a demo-appropriate approach rather than a
claim of full production migration tooling (a real deployment with multiple
concurrent schema changes across environments would want Alembic or
similar).

Usage:
    python3 migrate.py          # applies any migration not yet recorded as applied
    python3 migrate.py --status # shows which migrations have been applied
"""

import sys
from pathlib import Path

from schema import DB_PATH, get_connection

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

TRACKING_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     TEXT PRIMARY KEY,
    applied_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def applied_versions(conn) -> set[str]:
    conn.execute(TRACKING_TABLE_SQL)
    conn.commit()
    return {row["version"] for row in conn.execute("SELECT version FROM schema_migrations").fetchall()}


def available_migrations() -> list[Path]:
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


def run_migrations():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = get_connection()
    already_applied = applied_versions(conn)

    for path in available_migrations():
        version = path.stem
        if version in already_applied:
            print(f"  [skip]  {version} (already applied)")
            continue
        print(f"  [apply] {version}")
        conn.executescript(path.read_text())
        conn.execute("INSERT INTO schema_migrations (version) VALUES (?)", (version,))
        conn.commit()

    conn.close()
    print(f"Migrations complete. DB at {DB_PATH}")


def show_status():
    conn = get_connection()
    applied = applied_versions(conn)
    conn.close()
    for path in available_migrations():
        version = path.stem
        mark = "✓" if version in applied else " "
        print(f"  [{mark}] {version}")


if __name__ == "__main__":
    if "--status" in sys.argv:
        show_status()
    else:
        run_migrations()
