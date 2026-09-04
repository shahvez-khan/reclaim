# Bug Sweep Log

Tracking log for the looping bug-sweep-and-finalize prompt. Each entry is
one pass: one category, fully verified, committed.

## Status table

| # | Category | Status |
|---|---|---|
| 1 | Fresh-environment reproducibility | ✅ |
| 2 | CI pipeline, exactly as configured | ✅ |
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

**Commit:** 30e1107 — "Bug sweep pass 1 — fresh-environment
reproducibility: checked, clean"

### Pass 2 — CI pipeline, exactly as configured — 2026-09-04

**Environment notes:** This sandbox ships Python 3.12.3 as both `python`
and `python3`; Ubuntu 24.04's package repos (the only ones reachable —
network is restricted to a fixed allowlist that does not include
deadsnakes or similar PPAs) do not offer a `python3.11` package, and
`actions/setup-python@v5` isn't available outside real GitHub Actions.
So the one thing I could not match exactly is `.github/workflows/ci.yml`'s
pinned `python-version: "3.11"` — every step ran for real, just on 3.12
instead of 3.11. `requirements.txt`/`requirements-dev.txt` have no pinned
versions, so this is the most likely place a 3.11-vs-3.12 behavioral
difference would surface, and none did (see below), but a real CI run on
3.11 is the only way to fully close this out.

**What I checked:** Read `.github/workflows/ci.yml` and ran every step's
*exact* command, in the exact order given, from a freshly `git archive`'d
copy of the repo (independent of the `/home/claude/fresh` copy used in
Pass 1, so this pass didn't inherit any state), inside a clean Python venv
(`python3 -m venv` — the closest available stand-in for the isolation
`setup-python` + a fresh Actions runner gives you):

1. `pip install -r backend/requirements.txt` then `pip install -r
   backend/requirements-dev.txt` — both installed cleanly into the empty
   venv, no resolver conflicts, no build failures (fastapi, uvicorn,
   scikit-learn, pandas, numpy, joblib, pydantic, python-dotenv, pytest,
   ruff, sqlalchemy — the exact package set CI installs).
2. `ruff check backend/ ml/` — **"All checks passed!"**, exit 0. (The
   project's git log shows this step has been red before — from ruff's
   import-reordering side effect mentioned in the ground rules — so I
   didn't assume it was clean without actually running it.)
3. `cd ml && python generate_training_data.py && python
   train_recovery_model.py` — generated 6,334 training rows, trained
   both candidate models, selected logistic regression (ROC-AUC ~0.69),
   saved `recovery_model.pkl` / `model_metadata.json` to the repo-root
   `models/` dir — confirms the CI comment's stated reasoning (tests need
   a trained model importable before pytest runs) is actually satisfied
   by this step ordering.
4. `cd backend && pytest tests/ -v` — **39 passed, 0 failed**, ~25s.
   Same full pass count as Pass 1's separate `backend/tests/` run, this
   time using CI's own `cd backend && pytest tests/` invocation rather
   than `pytest backend/tests/` from the repo root — both resolve to the
   same 39 collected tests via `pyproject.toml`'s pytest config, no
   surprises from the different rootdir.

Also checked for anything that would make a real Actions checkout differ
from my `git archive` copy: `git status --short` is clean and `git
ls-files` contains no committed cache/build artifacts (`__pycache__`,
`.pyc`, `.pytest_cache`, etc.) that could mask a first-run failure a real
checkout would hit.

**Found:** Nothing wrong in the workflow file or the code it exercises.
The five steps are correctly ordered (install → lint → train → test, with
the model genuinely required by import-time code in the test-covered
modules, matching the comment's own justification), and every command
that's actually in the YAML ran without modification and passed.

**Fixed:** N/A — no changes made.

**Verified:** All five steps' exact commands were executed for real, in
order, in an isolated fresh checkout + clean venv, with full output
captured above (ruff's own pass/fail line, the training script's own
saved-model confirmation, pytest's own 39-passed summary) — not inferred
from Pass 1's results or from reading the code.

**Could not verify:** The workflow pins Python 3.11 (`actions/setup-python
@v5`, `python-version: "3.11"`); this sandbox can only run 3.12, and has
no path to installing 3.11 (restricted network, no deadsnakes PPA, Ubuntu
24.04 doesn't ship 3.11 in its default repos). Nothing in the dependency
set is version-pinned, so a 3.11/3.12 divergence is unlikely but not
provably ruled out here — running this workflow for real on
`push`/`pull_request` (or locally with an actual 3.11 interpreter
available) is the only way to close this out completely.

**Commit:** (this commit) — "Bug sweep pass 2 — CI pipeline exactly as
configured: checked, clean (Python 3.11 pin unverifiable in this sandbox)"
