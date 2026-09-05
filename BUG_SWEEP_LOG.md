# Bug Sweep Log

Tracking log for the looping bug-sweep-and-finalize prompt. Each entry is
one pass: one category, fully verified, committed.

## Status table

| # | Category | Status |
|---|---|---|
| 1 | Fresh-environment reproducibility | ✅ |
| 2 | CI pipeline, exactly as configured | ✅ |
| 3 | Docker build & run | ❓ |
| 4 | API input validation & error handling | 🛠️ |
| 5 | SQL / injection / secrets review | 🛠️ |
| 6 | Concurrency & idempotency | 🛠️ |
| 7 | Financial/data-integrity invariants | ✅ |
| 8 | Frontend robustness | 🛠️ |
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

**Commit:** a1112d3 — "Bug sweep pass 2 — CI pipeline exactly as
configured: checked, clean"

### Pass 3 — Docker build & run — 2026-09-04

**Environment notes:** No Docker daemon available in this sandbox (`docker`
is not an installed command at all, `docker --version` fails with "not
found"). Per the loop prompt's explicit instruction for this category, I
did **not** mark this ✅ or 🛠️ — I did the most rigorous static review I
could instead, and this is honestly marked ❓.

**What I checked:** Read `Dockerfile`, `docker-compose.yml`,
`docker-entrypoint.sh`, and `.dockerignore` line by line, then cross-checked
every path and env var each one references against what the actual
application code (`config.py`, `schema.py`, `db.py`, `api.py`,
`train_recovery_model.py`, `generate_training_data.py`) does with it:

- **Path anchoring, not cwd-dependent:** `config.py`'s `PROJECT_ROOT =
  Path(__file__).parent.parent` resolves to `/app` regardless of the
  entrypoint's various `cd` calls, and `DB_PATH`, `MODEL_PATH`,
  `MODEL_METADATA_PATH`, `MODEL_SCALER_PATH`, `LOG_DIR` are all built from
  it. `train_recovery_model.py` and `generate_training_data.py` do the
  same independently (`Path(__file__).resolve().parent.parent`), and
  `api.py`'s `FRONTEND_DIR` is anchored off `Path(__file__).parent` too.
  Traced every one of these against the entrypoint's own file-existence
  checks (`/app/data/revenue_recovery.db`, `/app/models/recovery_model.pkl`)
  and the compose file's volume mounts (`./data:/app/data`,
  `./models:/app/models`, `./logs:/app/logs`) — all four agree on the same
  absolute in-container paths. This is exactly the CWD-dependent-path bug
  class flagged elsewhere in this project's history, and I looked for it
  specifically here; it doesn't recur in the Docker path either.
- **Dependency surface:** the image only installs `backend/requirements.txt`
  (not `requirements-dev.txt`, so no `sqlalchemy`/`pytest`/`ruff` in the
  runtime image). Confirmed `db.py` — the one module that imports
  `sqlalchemy` — is (a) never imported by any runtime module (`api.py`,
  `run_pipeline.py`, `migrate.py`, `generate_data.py`, or anything they
  import), and (b) does its `sqlalchemy` import lazily inside
  `get_engine()`, not at module level. So a production image without dev
  deps genuinely can't crash on that import, by construction.
- **Entrypoint bootstrap order:** `docker-entrypoint.sh` runs `migrate.py`
  → `generate_data.py` (only if no DB file exists) → ml training (only if
  no model file exists) → `exec uvicorn`. Confirmed `api.py` has no
  startup hook that calls `run_pipeline.py` itself — the decision/execution
  step only runs via the `/api/run-batch` endpoint (the dashboard's
  "Re-run batch" button), matching what the README's Docker section
  actually claims ("bootstraps data + trains the model... then serves the
  API + dashboard") — it does not claim the pipeline itself auto-runs, so
  I didn't hold it to a claim it doesn't make. First-load `/api/summary`
  would show real raw records with zero decisions yet (all zero
  recovered), which is a sensible, non-crashing empty-ish state, not a bug.
- **Healthcheck command:** `["CMD", "python", "-c", "import
  urllib.request; ..."]` — the official `python:3.11-slim` base image does
  provide an unversioned `python` binary (unlike bare Debian), so this
  resolves correctly; confirmed by checking the base image's known layout
  rather than assuming.
- **`.dockerignore`** correctly excludes `data/`, `models/`, `logs/`,
  `.git/`, `.github/`, `backend/tests/`, `.env`, and cache dirs — nothing
  the runtime needs is excluded, and nothing large/irrelevant is shipped
  into the build context.

**Found:** Nothing, via static review. Every path and env var traced
consistently end-to-end across all four Docker-related files and the
application code they drive.

**Fixed:** N/A.

**Verified:** This is explicitly *not* a run-verified result — see below.
The cross-checks above were done by reading source, not by executing
`docker build`/`docker compose up`.

**Could not verify:** The actual `docker build` (does the image build at
all — base image pull, layer caching, `pip install` inside the container's
own network/DNS), `docker compose up` (do the bind-mount volumes actually
get created and populated as expected, does the entrypoint script's `set
-e` + conditionals behave as intended when actually executed by `/bin/sh`
inside the container), hitting the health endpoint and real routes through
the running container, and a `down` + `up` cycle to confirm persisted
volume state survives a restart — **none of this was actually run**, since
no Docker daemon is reachable in this sandbox. I also could not time the
healthcheck's `start_period: 30s` against the real bootstrap duration
inside an actual container (a different runtime environment than this
sandbox's raw Python). This category needs an environment with Docker
available to close out for real — running `docker compose up` once, end to
end, would settle it.

**Commit:** 454bbea — "Bug sweep pass 3 — Docker build & run: static
review only, no Docker daemon available in this sandbox"

### Pass 4 — API input validation & error handling — 2026-09-04

**Environment notes:** None beyond earlier passes. Server run against the
same `/home/claude/fresh` copy set up in Pass 1 (real DB, real trained
model), plus the repo working copy itself for the final fix + test run.

**What I checked:** For every endpoint in `api.py`, sent missing/empty
bodies, wrong-typed fields, out-of-range and nonexistent IDs, empty-string
IDs, SQL-injection-style path segments, non-ASCII/unicode input, and an
oversized payload — then, per this category's explicit instruction,
cross-checked every response shape against what `frontend/app.js` actually
does with it, since a correct backend error is only real protection if the
frontend doesn't ignore it.

- `POST /api/escalations/{id}/resolve`: nonexistent/non-numeric/negative
  IDs all correctly 404. Wrong-typed `resolver_note` (number, array)
  correctly 422 with a useful Pydantic error body. Unicode/emoji
  (`已解决 ✅ — résumé`) round-trips exactly. **Found:** a 200KB
  `resolver_note` was accepted and stored verbatim — no length limit
  anywhere in the request model.
- `GET /api/records`: invalid `status`/`record_type` values correctly 400
  with the exact valid-set listed; nonexistent/empty-string `batch_id`
  correctly 404; unicode in filters correctly 400, not silently ignored.
- `GET /api/audit/{record_id}`: nonexistent id → 404; oversized (200-char)
  id → 400 (existing length guard); SQL-injection-style id (`x'; DROP
  TABLE transactions; --`) as a real path segment → clean 404, and
  confirmed via a follow-up query that the `transactions` table still has
  all 200 rows — parameterized queries hold. Unicode id → clean 404.
- `GET /api/escalations`, `GET /api/summary`, `GET /api/baseline`: all
  filter/enum validation already correct and already had before-this-pass
  coverage; `GET /api/baseline`'s deliberate "only the current batch" 400
  is not just correctly implemented on the backend — I confirmed
  `app.js`'s `loadBaselineComparison()` specifically special-cases that
  exact 400 into a friendly, non-error message, matching the backend's
  intent precisely. Good existing cross-check, nothing to fix there.
- **`POST /api/run-batch` → frontend `runBatchBtn` click handler: found and
  fixed a real bug.** The backend correctly returns 409 (concurrent run
  already in progress), 429 (rate limit — 3 runs / 60s), or 500 (pipeline
  crash) via `HTTPException`, all reshaped by the app's global handler into
  `{"error": {"status", "message", ...}}`. But `app.js`'s click handler did
  `const data = await res.json()` and used `data.batch_id` **without ever
  checking `res.ok`** — so on any of those three failure responses,
  `data.batch_id` was `undefined`, `currentBatchId` silently became `null`,
  and the handler proceeded to refresh the dashboard and set the button
  back to "Re-run batch" (its success text) as if the run had actually
  happened. A user (or two open tabs) triggering the exact concurrency
  guard verified working in Pass 1 would see no indication whatsoever that
  their click did nothing.

**Fixed:**
1. `frontend/app.js`'s `runBatchBtn` handler now checks `res.ok` before
   trusting the body, parses the `{"error":{"message":...}}` shape the
   backend actually sends, and throws (routing into the existing `catch`
   → `"Failed — retry"` button state) instead of silently treating any
   non-2xx response as success. `currentBatchId` and the rest of the
   dashboard are now left untouched on failure instead of being reset to
   `null` and re-fetched with stale/wrong context.
2. `backend/api.py`'s `ResolveEscalationRequest.resolver_note` now has
   `max_length=2000` (a Pydantic `Field` constraint) — generous for a
   human's one-or-two-sentence resolution note, closing the
   unbounded-write gap found above.

**Verified:**
- **Proved the frontend bug existed first**, per the ground rules: wrote a
  Node script (`/tmp/repro_bug.mjs`) that replays the *original*
  click-handler logic verbatim against the real running server, fired two
  concurrent `run-batch` calls (the same scenario Pass 1 used to prove the
  backend's lock works), and confirmed the losing call's `httpStatus: 409`
  response still produced `wouldShow: "Re-run batch"` (the success text)
  with the original code — the bug reproduces, not just reasoned about.
- Re-ran the same replay with the *fixed* logic (`/tmp/verify_fix.mjs`)
  against the real server: this time a rejected call (429, rate-limited
  from the repro run moments earlier) correctly produced
  `finalBtnText: "Failed — retry"` with the specific server message
  surfaced (`"Rate limit exceeded: max 3 batch runs per 60s..."`) and
  `currentBatchId` left unchanged — confirming the fix closes the gap for
  every non-2xx status, not just the one status I happened to trigger in
  the original repro.
- Waited out the rate-limit window and re-ran a lone successful call with
  the fixed logic (`/tmp/verify_success.mjs`) — confirmed the success path
  is completely unaffected: `finalBtnText: "Re-run batch"`,
  `currentBatchId` set to the real new batch id.
- For the `resolver_note` fix: confirmed a 200KB note is now rejected with
  `422 string_too_long`, and confirmed a normal-length note still resolves
  the escalation successfully (200, note stored and echoed back exactly).
- Ran the full backend suite twice — once in the `/home/claude/fresh`
  copy, once in the actual repo working copy that was committed — **39
  passed, 0 failed** both times, confirming neither fix broke anything
  covered by existing tests (none of which happened to exercise this exact
  gap, which is itself worth knowing for Pass 9).

**Could not verify:** No frontend test suite exists in this project (pure
backend `pytest`, nothing under a JS test runner), so the frontend fix's
verification is the Node-script replay above, not an automated regression
test added to CI — a real browser click-through would be the fullest
verification, which this sandbox can't do. Also noted, but deliberately
**not fixed this pass** (would be scope creep beyond one root-caused
bug per pass): `frontend/app.js`'s `loadHeroExamples()` is the only loader
in the file with no `try/catch` at all (every sibling loader has one with
a friendly fallback UI) — if `/api/hero-examples` fails, it's an unhandled
promise rejection rather than a visible error state. It's low-severity
(fire-and-forget, doesn't block other sections) and overlaps with Category
8's charter (frontend robustness) more than this one, so I'm flagging it
here for that pass rather than fixing it now. Also noted: `label`/`blurb`
in the same endpoint's frontend render skip the `escapeHtml()` helper used
everywhere else — but I traced the backend source and confirmed both
fields are 100%-hardcoded constant strings (never derived from any
customer/user data), so there's no actual injection path today; flagging
as a defensive-coding inconsistency for Category 8 rather than a real
vulnerability.

**Commit:** (this commit) — "Bug sweep pass 4 — API input validation:
fix run-batch frontend silently treating 409/429/500 as success, cap
resolver_note length"

### Pass 5 — SQL / injection / secrets review — 2026-09-04

**Environment notes:** None beyond earlier passes.

**What I checked:**

- **Non-parameterized SQL:** grepped the whole repo for
  `execute(f"..."`/`.format(`/`% (` patterns used to build SQL. Found four
  call sites (`decision.py:379`, `execution.py:320`, and two in
  `backend/tests/test_batch_durability.py`) — all four only use string
  building to construct the `?` *placeholder count* for a variable-length
  `IN (...)` clause (e.g. `",".join("?" * len(types))`), with the actual
  values always passed as bound parameters afterward. Traced `types` back
  to its single definition in both `decision.py`/`execution.py`: it's a
  hardcoded default tuple (`("transaction","receivable","abandonment")`)
  that no caller anywhere overrides — never reachable from any API
  endpoint or external input. The two test-file cases interpolate literal,
  hardcoded table names from a fixed tuple, never external input. All four
  match this project's own documented legitimate pattern (see the ground
  rules' note about a table/column name from a constrained enum); nothing
  to fix.
- Broader sweep of every remaining `cur.execute(`/`conn.execute(` call
  across `api.py`, `schema.py`, `agent_loop.py`, `decision.py`,
  `execution.py`, `generate_data.py`, `run_pipeline.py`, `migrate.py` —
  confirmed every one uses `?` placeholders with values passed as a
  separate tuple, including `api.py`'s dynamically-built `/api/escalations`
  query (appends static SQL text conditionally, never the value itself).
  Specifically re-tested a SQL-injection-style payload
  (`x'; DROP TABLE transactions; --`) as a real `/api/audit/{id}` path
  segment (carried over from Pass 4's testing) and confirmed the
  `transactions` table was untouched.
- **Secrets:** grepped for `api[_-]?key|secret|password|token|razorpay`
  across all source, config, and doc files. `RAZORPAY_API_KEY` /
  `RAZORPAY_WEBHOOK_SECRET` are read in `config.py` but both default to an
  empty string, are documented (in `.env.example`, `DATA_SOURCES.md`,
  `execution.py`'s `RazorpayExecutor` docstring) as unused placeholders for
  a not-yet-implemented integration, and are never non-empty anywhere —
  confirmed via `git log -p --all -- .env.example`, which shows only ever
  empty values committed, and confirmed `.env` (the real, filled-in file a
  developer would create) has never been committed at any point in this
  repo's history (`git log --all --diff-filter=A --name-only | grep
  '^\.env$'` → no results).
- **`.env.example` vs `config.py` sync:** extracted every `VAR=` in
  `.env.example` and every `os.environ.get("VAR")` in `config.py` and
  diffed the two sets — **exact match**, no drift in either direction.
- **Error-response leakage:** checked every exception path for stack
  traces, file paths, or row-level data reaching the client. The global
  `Exception` handler and `/api/health`'s handler both correctly log the
  real error server-side (`logger.exception` / `logger.error`) and return
  only a generic message to the client — good existing design, confirmed
  by reading, not just assumed. **Found:** `POST /api/run-batch`'s failure
  path did the opposite — it logged only `run_id`/`returncode` server-side
  but put the **full raw subprocess stderr** (up to 2000 chars — a
  complete Python traceback, including absolute filesystem paths like
  `/app/backend/run_pipeline.py`, internal module/function names, and
  library internals) directly into the client-facing HTTP response body.
- Checked `schema.py`'s table definitions for any sensitive column types
  (password/SSN/card-number/CVV/secret/token-shaped columns) — none exist;
  this is a synthetic-data demo with no real PII by design (see
  `DATA_SOURCES.md`), so there's no row-level sensitive data to leak in the
  first place.

**Found:**
1. `.gitignore` has no `.env` entry (only `.env.example`, the safe
   template, is tracked) — so once someone actually fills in a real
   `RAZORPAY_API_KEY`/`RAZORPAY_WEBHOOK_SECRET` for the real integration
   the codebase is explicitly structured to support later (per
   `execution.py`'s `RazorpayExecutor` docstring and `.env.example`'s own
   comments), a routine `git add .`/`git add -A` would stage and commit
   it. `.dockerignore` already excludes `.env` — `.gitignore` was the one
   gap. No secret has actually been committed to date (confirmed via git
   history above), but the guard rail that would prevent it from happening
   the moment someone does the realistic next thing was missing.
2. `POST /api/run-batch`'s 500 response leaked the full pipeline
   traceback to the client (see reproduction below) instead of just to
   the server log — a textbook internal-state-disclosure gap for this
   category's checklist.

**Fixed:**
1. Added `.env` to `.gitignore`.
2. `backend/api.py`'s `run_batch()`: server log now captures the full
   `result.stderr` (last 4000 chars, extra logging field) on failure; the
   client-facing `HTTPException` detail is now a generic
   `"Batch run failed (run_id=...). Check server logs for details."`,
   correlatable via `run_id` without exposing any internals.

**Verified:**
- **Proved the `.gitignore` gap first**, per the ground rules: in an
  isolated scratch git repo, checked out the *old* `.gitignore` (from
  commit `fa4f4a7`, pre-fix) alongside a real `.env` file with the exact
  placeholder content `.env.example` documents, and ran `git add -A -n`
  (dry run) — output included `add '.env'`, confirming it would have been
  staged. Swapped in the *new* `.gitignore` and re-ran the same dry run —
  `.env` no longer appears in the output (only `.env.example` and the
  gitignore file itself do), confirming the fix.
- **Proved the leakage bug first**, per the ground rules: in the
  `/home/claude/fresh` test copy, temporarily moved
  `models/recovery_model.pkl` out of the way, hit `POST /api/run-batch`
  against the *original* code, and captured the actual HTTP response —
  it contained the complete raw traceback verbatim, including six
  absolute `/home/claude/fresh/...` file paths and the exact
  `FileNotFoundError` message. Applied the fix, restarted the server, and
  reproduced the *identical* failure again (same missing-model trigger) —
  this time the client response was
  `{"error":{"status":500,"message":"Batch run failed (run_id=...). Check
  server logs for details.","detail":null}}`, no path or traceback content
  at all. Then confirmed the server's own JSON log line for that same
  failure still contains the full 1,689-character stderr (including the
  exact `FileNotFoundError` and path) under a `stderr` field — so nothing
  was lost for real debugging, it just stopped being sent to the client.
  Restored the model file after each test.
- Ran the full backend suite in both the `/home/claude/fresh` copy and the
  actual repo working copy that was committed: **39 passed, 0 failed**
  both times. Also re-ran `ruff check .` on the committed copy — all
  checks passed.

**Could not verify:** N/A for this pass — both findings were reproduced
and both fixes verified against a real running server and a real (scratch)
git repository, not just reasoned about.

**Commit:** (this commit) — "Bug sweep pass 5 — SQL/secrets review: add
.env to .gitignore, stop leaking pipeline tracebacks in run-batch's 500
response"

### Pass 6 — Concurrency & idempotency — 2026-09-04

**Environment notes:** None beyond earlier passes.

**What I checked:** `run_pipeline.py`'s own docstring makes an explicit
correctness claim: "the real production risk of calling /api/run-batch
twice concurrently is two pipeline runs processing (and
double-charging/double-messaging) the same records at once... this is
guarded with a simple file-based lock: a second concurrent
run_full_pipeline() call fails fast." I read `_acquire_lock()` to check
whether that guarantee actually holds under real concurrent load, per this
category's explicit instruction not to just reason about locks but to
spawn real parallel callers.

The original implementation was:
```python
if PIPELINE_LOCK_PATH.exists():
    ...raise...
PIPELINE_LOCK_PATH.write_text(str(os.getpid()))
```
— a classic check-then-act sequence: two separate statements, not one
atomic operation.

**First attempt (and an honest note on a flawed methodology):** I first
tested this with 30 `multiprocessing` processes released via a
`Barrier`, each holding the lock for only 0.05s if acquired. This showed
2–5 of 30 "acquiring" the lock per trial — but re-examining that result,
I realized it was a **measurement artifact, not a real race**: with 30
freshly-forked processes each importing seven heavy modules
(scikit-learn, pandas, etc.), the *arrival* at the lock-check line is
staggered by tens of milliseconds across processes — comfortably longer
than the 0.05s hold — so a "second acquirer" was often just legitimately
reacquiring a lock the first holder had already released, not racing it
at the same instant. Re-running the identical test with a 3-second hold
(long enough that arrival-time jitter can no longer matter) gave a clean
1-of-30 every trial, on **both** the original and fixed code — meaning my
first test didn't actually prove what I initially wrote. Flagging this
here explicitly per the ground rules ("don't trust a prior pass's summary
text over your own fresh verification" applies to my own mid-pass work
too) rather than quietly discarding the mistake.

**Second attempt — a methodologically sound proof:** the standard
technique for proving a TOCTOU (time-of-check-to-time-of-use) bug is real,
independent of how fast a given machine happens to execute the two
statements, is to artificially widen the gap between the check and the
act with a sleep — standing in for a context switch, GC pause, scheduler
preemption, or slow filesystem landing at that exact point, none of which
the original code has any defense against. I wrote `/tmp/prove_toctou.py`:
10 processes released simultaneously via a `Barrier`, each running the
*exact* original check-then-act logic with a 0.3s sleep inserted between
the `exists()` check and the write. Result: **10 of 10 processes
"acquired" the lock** — conclusively proving the file-existence check
provides no actual synchronization; any real-world delay of a few hundred
milliseconds between the two statements (plausible under real load, a
busier disk, or a container host under CPU pressure) would let multiple
pipeline runs process the same records concurrently, exactly the
double-charging/double-messaging risk the docstring claims is prevented.

**Found:** `run_pipeline.py`'s `_acquire_lock()` is a genuine
check-then-act race, not an atomic lock, contradicting its own docstring's
correctness claim.

**Fixed:** Rewrote `_acquire_lock()` to use `os.open(path, os.O_CREAT |
os.O_EXCL | os.O_WRONLY)` — atomic at the OS/kernel level; POSIX
guarantees only one caller can win the create when multiple race it, with
no window between "check" and "act" because there is no separate check —
the attempt-and-claim happen in one syscall. Preserved the existing
stale-lock-reclaim behavior (a lock file older than 600s is treated as a
crashed prior run) with a bounded retry loop for the much narrower,
lower-stakes race of two processes reclaiming the *same* stale lock at
once.

**Verified:**
- Re-ran `/tmp/prove_toctou.py`'s identical sleep-widening technique
  against the **fixed** logic (same artificial delay, inserted at the
  analogous point right before the `os.open` call): **1 of 10** acquired,
  every time — proving the atomicity comes from the syscall itself and is
  unaffected by what runs around it, not from lucky timing.
- Re-ran the corrected (3-second hold, no false-positive-prone short
  hold) 30-process concurrent test from the first attempt against the
  fixed code: **exactly 1 of 30** acquired simultaneously, consistent
  across 5 trials.
- Ran the full backend suite in both the `/home/claude/fresh` copy and
  the actual repo working copy that was committed: **39 passed, 0
  failed** both times. Re-ran `ruff check` on the committed copy: all
  checks passed.

**Could not verify:** N/A for the core finding — both the bug and the fix
were demonstrated conclusively via the sleep-widening technique, not just
reasoned about. Separately noted but **not fixed this pass** (would be a
second, lower-priority finding diluting focus from the one above): `api.py`'s
in-memory rate limiter for `/api/run-batch` (`_run_batch_calls`) has the
same check-then-act shape (`if len(...) >= MAX: raise` then `.append(now)`)
across the thread pool FastAPI's sync endpoints run on — under real thread
contention this could let one or two extra calls through the limit
briefly. This is materially lower-stakes than the pipeline lock (the code
already documents this limiter as "a minimal in-memory... stand-in" for a
future Redis-backed limiter, not a hard guarantee like the pipeline lock's
explicit double-processing-prevention claim), so fixing it is left for a
future pass rather than expanding this one.

**Commit:** (this commit) — "Bug sweep pass 6 — concurrency: fix a real
TOCTOU race in the pipeline run-lock (proven via sleep-widening, not
just reasoned about)"

### Pass 7 — Financial/data-integrity invariants — 2026-09-05

**Environment notes:** None beyond earlier passes. Used the
`/home/claude/fresh` copy, which by this point had 10 batches accumulated
from earlier passes' testing plus 3 more freshly triggered in this pass
(via real `POST /api/run-batch` calls, not by re-reading old data) — 13
independent pipeline runs in total to check invariants against, not just
one.

**What I checked:** Wrote `/tmp/reconcile.py`, which re-derives every
headline number **directly from raw SQL against the live DB** — not via
the API or any of the project's own summary-computing code — for every
batch that has ever run, and checks:

1. **Parts sum to the whole:** blended `total_recovered` ==
   `txn_recovered + recv_recovered + aband_recovered`, computed
   independently in the reconciliation script rather than trusting
   `api.py`'s own arithmetic.
2. **Recovered never exceeds at-risk**, overall and per record-type
   (transactions, receivables, abandonments) separately.
3. **Bucket counts cross-foot exactly** to `total_records`
   (`recovered + escalated + still_failing + stopped_no_action ==
   total_records`), independently reconstructing the same bucketing logic
   `api.py` uses, from raw `decisions`/record rows.
4. **The escalations table matches `escalate_to_human` decisions
   exactly**, per record type and per batch — mirroring the exact
   cross-footing check this project's own history already established
   elsewhere (per the category's own example).
5. **No abandonment ever has `recovered_amount` exceeding `cart_value` or
   negative** — checked directly against all 198 abandonments with a
   non-null recovered amount across the DB.
6. **Every record marked `status='recovered'` has at least one real
   `decisions` row behind it** — no "recovered" status floating free of
   any decision that could have produced it (checked separately for all
   three record types).
7. **`audit_log` cross-foots against `decisions` + `REPLANNED` markers**:
   for the transaction pipeline specifically (which logs a separate
   `REPLANNED` audit event in addition to each attempt's outcome event),
   confirmed `audit_log` count (1048) == `decisions` count (924) +
   `replanned_count` (124) **exactly**, tracing this from `agent_loop.py`'s
   actual insert logic rather than just noticing the numbers happened to
   match.
8. **Cross-checked `/api/summary`'s actual live JSON response** against
   the same raw-SQL derivation for one of the freshly-generated batches —
   `total_at_risk`, `total_recovered`, and `bucket_counts` matched exactly
   (₹106,795,396.81 at risk, ₹21,784,845.57 recovered, 230/233/265/72
   summing to 800).
9. **`/api/baseline` determinism**: the "recomputed fresh each run, not
   retained per historical batch" phrasing in its own 400 error raised a
   real question worth checking — does "recomputed fresh" mean the
   underlying comparison involves live randomness, so two reads of the
   same current batch could show a stakeholder a different "incremental
   value" number on a page refresh? Called `/api/baseline` three times in
   a row for the same current batch: byte-identical JSON every time.
   "Recomputed fresh" means computed from stored decisions on each read
   rather than cached, not non-deterministic — confirmed, not just
   assumed from the wording.
10. Ran this full reconciliation against all 13 batches, including 3
    newly triggered via real `POST /api/run-batch` calls during this pass
    (not just replaying the same historical batches every prior pass had
    already touched) — every single one reconciled exactly, satisfying
    this category's "run multiple times, confirm every invariant holds
    every time" instruction.

**Found:** Nothing. Every invariant held, on every batch checked,
independently re-derived from raw SQL rather than trusted from the
project's own computation. One thing I noticed but want to be precise
about **not** over-claiming as a data-integrity bug: `/api/baseline`'s
`abandonments.incremental` was negative (-₹33,110) in the batches checked
— the agent recovered *less* than the simulated baseline for abandonments
specifically, while doing better for transactions and receivables. This is
a correctly-computed number (the confidence interval correctly includes
zero and `recovery_rate_delta_significant` is correctly `false`), not an
impossible or inconsistent one — it reflects the agent's decision policy
underperforming baseline for that one record type, which is a product/
modeling observation, not a violated invariant. Flagging it here for
visibility rather than silently noticing and moving on, but not treating
it as a "found" bug for this category — whether the README/DEMO_SCRIPT
overstate uniform improvement across all three record types belongs to
Category 10 (documentation accuracy), where I'll check the exact wording
against this number.

**Fixed:** N/A — no changes made this pass.

**Verified:** All of the above was computed by direct SQL query against
the live database in a throwaway script, not by reading and trusting the
application's own summary code, and cross-checked against the live
`/api/summary` and `/api/baseline` HTTP responses for at least one batch
to confirm the API layer isn't silently diverging from what's actually in
the database. Ran the full backend suite once more (no code changed this
pass): **39 passed, 0 failed**.

**Could not verify:** N/A — this pass was pure verification against live
data with no environment-dependent gaps.

**Commit:** (this commit) — "Bug sweep pass 7 — financial/data-integrity
invariants: checked, clean across 13 independent batches"

### Pass 8 — Frontend robustness — 2026-09-05

**Environment notes:** No browser available in this sandbox. Verification
uses the same technique as Pass 4's frontend fix: replaying `app.js`'s
actual logic verbatim in a Node script against the real running server (or
a deliberately-killed one), since that exercises the real fetch/parse/
error-handling code paths rather than reasoning about them from the source
alone. This closes out two items explicitly deferred here from Pass 4.

**What I checked:**
- Grepped every `innerHTML =` assignment in `app.js` (23 call sites) and
  checked each dynamic value against `escapeHtml()` usage. All render
  functions except one (`loadHeroExamples`) route every DB-sourced field
  through `escapeHtml()` — records table, escalations list, receipt/audit
  view, batch dropdown, stopping-rule and failure-code breakdowns all
  escape consistently.
- Grepped every `fetch(...)` call site and checked for a `res.ok` check
  before the body is treated as trusted JSON. Found exactly one gap:
  `loadHeroExamples()` — the same function flagged (but deliberately left
  for this pass) in Pass 4.
- **Reproduced the bug for real**, per the ground rules: confirmed
  `/api/hero-examples?batch_id=<unknown>` actually 404s
  (`{"error":{"status":404,...}}`), then replayed `loadHeroExamples()`'s
  *original* logic verbatim in a Node script against that real response —
  it threw `TypeError: examples.map is not a function`, because the code
  read the error body as if it were the expected array (`examples.length
  === 0` silently evaluated to `undefined === 0` → false, falling through
  to `.map()` on a plain object). This is a realistic failure mode, not a
  contrived one: `currentBatchId` is a plain JS global that can go stale
  relative to the server (e.g., another tab or a server-side reseed
  changes which batches exist).
- **Simulated "kill the backend mid-load, reload the page"**: killed the
  real uvicorn process, then replayed both the pre-existing
  `loadSummary`-style logic and the (at-that-point-still-original)
  `loadHeroExamples` logic against the dead server — confirmed the network
  failure (`fetch failed`) is caught internally by every loader that has a
  `try/catch`, which was every loader except `loadHeroExamples`.
- Checked empty-list handling elsewhere: `loadRecords` shows "No records
  in this bucket", `loadEscalations` shows "No open escalations", batch
  dropdown shows "No batches yet" — all already correctly guarded, not
  just assumed to be.
- Checked `renderReceipt`'s null-safety around optional fields
  (`diagnosis`, `dec.retry_at`, `dec.stopping_rule_fired`, `outcome`) —
  all conditionally rendered with a `—` fallback, not accessed unguarded.

**Found:** `loadHeroExamples()` was the only loader in the file with (a)
no `try/catch` at all, and (b) no `res.ok` check — so any non-2xx response
(stale batch_id, backend down, backend restarting) produced an uncaught
`TypeError` instead of the friendly degraded state every other section of
the dashboard shows. Also, as flagged in Pass 4: its render skipped
`escapeHtml()` for `label`/`blurb`/`record_id`, inconsistent with every
other render function in the file (no live injection path today, since
Pass 4 confirmed those specific fields are hardcoded backend constants —
but inconsistent defensive coding that would become a real gap the moment
that endpoint's data source changes).

**Fixed:** Rewrote `loadHeroExamples()` to match the file's established
pattern: wrapped in `try/catch`, checks `res.ok` before trusting the body,
and routes `label`/`blurb`/`record_id` through `escapeHtml()` like every
sibling render function. On failure, it clears the row quietly (logging to
console) rather than showing a visible error banner — a deliberate choice,
not an oversight: this widget is a supplementary "walk through a live
example" prompt, not critical data, so a quiet degrade is more appropriate
than a red error banner competing for attention with the real dashboard
sections that do use the louder "Couldn't load X. Retry" pattern.

**Verified:**
- Replayed the **fixed** `loadHeroExamples()` logic against the exact
  previously-crashing scenario (`batch_id=nonexistent_batch` → 404):
  `CAUGHT: HTTP 404 (row cleared, no crash)` — no exception escapes.
- Replayed the same fixed logic against a normal successful call (current
  batch, no `batch_id` param): correctly renders all three example labels,
  unaffected by the fix.
- Sanity-checked `escapeHtml()` against a real injection payload
  (`<img src=x onerror=alert(1)>`) → correctly neutralized to
  `&lt;img src=x onerror=alert(1)&gt;`.
- Re-grepped every `fetch(...)` call site after the fix: all now check
  `res.ok` before parsing — the hero-examples gap was the last one.
- Killed the real backend again post-fix and replayed the fixed logic:
  network failure caught internally, consistent with every other loader.
- Ran the full backend suite (no backend code changed this pass, frontend
  has no automated test suite to run): **39 passed, 0 failed** — confirms
  the frontend-only change didn't require or break any backend contract.

**Could not verify:** No real browser is available in this sandbox, so
this is Node-script replay of the actual `app.js` logic against a real
server, not a true end-to-end click-through in a rendered page (same
caveat as Pass 4's frontend fix). A real browser session — checking the
DOM actually updates correctly, no console errors on a live reload, etc.
— would be the fullest verification.

**Commit:** (this commit) — "Bug sweep pass 8 — frontend robustness: fix
loadHeroExamples' missing error handling and escaping (reproduced the
crash first)"
