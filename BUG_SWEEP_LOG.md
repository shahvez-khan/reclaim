# Bug Sweep Log

Tracking log for the looping bug-sweep-and-finalize prompt. Each entry is
one pass: one category, fully verified, committed.

## Status table

| # | Category | Status |
|---|---|---|
| 1 | Fresh-environment reproducibility | ✅ |
| 2 | CI pipeline, exactly as configured | ⬜ |
| 3 | Docker build & run | ⬜ |
| 4 | API input validation & error handling | ⬜ |
| 5 | SQL / injection / secrets review | ⬜ |
| 6 | Concurrency & idempotency | ⬜ |
| 7 | Financial/data-integrity invariants | ⬜ |
| 8 | Frontend robustness | ⬜ |
| 9 | Test suite quality | ⬜ |
| 10 | Documentation accuracy | ⬜ |
| 11 | Dead code / stale comments / final polish | ⬜ |

## Pass history

### Pass 1 — Fresh-environment reproducibility — 2026-09-04

**Environment notes:** Sandboxed Linux container, Python 3.12.3, no Docker
daemon available (relevant for a later pass, not this one). Network access
to pypi.org is available, so `pip install` works normally.

**What I checked:** Started from a truly clean copy of the repo made with
`git archive HEAD | tar -x` into a fresh directory (`/home/claude/fresh`),
distinct from the working tree, containing no `data/`, `models/`, `logs/`,
or `ml/training_data.csv` — confirmed these are all gitignored and genuinely
absent, not just present-but-empty. Then followed the README's
**Installation → Manual** section literally, line by line, from that clean
copy, with no deviation:

1. `cd backend && pip install -r requirements.txt --break-system-packages`
   — installed fastapi, uvicorn, scikit-learn, pandas, numpy, joblib,
   pydantic, python-dotenv cleanly, no errors, no version conflicts.
2. `pip install -r requirements-dev.txt --break-system-packages` — pytest,
   ruff, sqlalchemy, python-dotenv installed cleanly.
3. `cp ../.env.example ../.env`
4. `python3 migrate.py` — applied all 5 migrations (0001–0005) in order
   from a cold DB, no errors. Confirms `migrate.py` correctly resolves its
   DB path when invoked from `backend/` as the README instructs (this is
   exactly the class of CWD-dependent-path bug the prompt calls out as
   having bitten this project twice before — checked for it here
   specifically and it's fine).
5. `python3 generate_data.py` — generated 200 transactions / 400
   receivables / 200 abandonments, ₹106,795,396.81 total at risk, matching
   the shape described in the README.
6. `cd ../ml && python3 generate_training_data.py && python3
   train_recovery_model.py` — generated 6,334 training rows, trained and
   saved the model to `../models/` (i.e. the repo-root `models/` dir, not
   `ml/models/` — resolves correctly from the `ml/` cwd as documented).
   ROC-AUC 0.6905 vs Random Forest's 0.688, logistic regression selected —
   matches the README's reported numbers closely (within run-to-run
   stochastic variation, as the README itself notes).
7. `python3 run_pipeline.py` (from `backend/`) — full pipeline ran
   end-to-end: diagnosis → agent loop → decisions → baseline for all three
   record types. Consistency check passed (`230 + 233 + 265 + 72 == 800`).
8. `python3 -m uvicorn api:app --host 0.0.0.0 --port 8000` then `curl
   http://localhost:8000/` → 200, dashboard HTML served. Hit
   `/api/health`, `/api/summary`, `/api/batches`, `/api/records?limit=2`,
   `/api/escalations` — all returned well-formed JSON with real data, no
   500s, no stack traces.
9. Exercised the demo's "Re-run batch" flow directly: `POST
   /api/run-batch` → 200 with a new `batch_id`. Fired two concurrent
   `POST /api/run-batch` requests to check the documented run-lock — one
   got `200`, the other correctly got `409` (idempotency guard works as
   claimed, not just asserted in prose).
10. Killed and restarted the uvicorn process, then re-queried
    `/api/batches` — all batches from every run in this session persisted
    across the restart (SQLite file `data/revenue_recovery.db`, not held
    only in memory) — confirms the "durable, additive" claim isn't
    contradicted by a restart.
11. Ran the full test suite exactly as documented: `pytest backend/tests/
    -v` → **39 passed**, 0 failed, 0 errors, ~24s. Also ran one test file
    via its plain-Python fallback runner (`python3
    tests/test_pipeline_invariant.py`, no pytest) as the README claims is
    possible — it worked and printed a clear PASS line.

**Found:** Nothing. Every documented installation step, in the documented
order, from a truly clean tree, worked exactly as written — no
CWD-dependent path bugs, no missing-directory assumptions, no stale
version pins, no drift between what the README says to run and what the
code actually does. This is the specific bug class the prompt calls out as
having bitten this project twice before, and I looked for it deliberately
(migrate.py's DB path, run_pipeline's relative model paths, the ml/ →
models/ path from Pass 0's fix); none of it recurred.

**Fixed:** N/A — nothing to fix. No code changes made in this pass.

**Verified:** All of the above was actually executed in a fresh directory
created via `git archive`, not reasoned about — every command's real
output is quoted above. The 39/39 pytest pass count and the 409 on the
concurrent run-lock test are both live results from this session, not
carried over from a prior report.

**Could not verify:** Docker build/run and the CI workflow exactly-as-
configured are explicitly out of scope for this category (they're
categories 2 and 3) and weren't touched here. No Docker daemon is
available in this sandbox, which will need to be flagged again when
category 3 comes up.

**Commit:** (this commit) — "Bug sweep pass 1 — fresh-environment
reproducibility: checked, clean"
