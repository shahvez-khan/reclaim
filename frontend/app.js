const API = "";

const fmtINR = (n) => "₹" + Number(n).toLocaleString("en-IN", { maximumFractionDigits: 0 });
const fmtINR2 = (n) => "₹" + Number(n).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

// Basic output sanitization (Phase 4.8): every record-derived string that
// gets interpolated into innerHTML goes through this first. Low real risk
// today (data is our own synthetic generator, not external user input), but
// this is the correct default for any dashboard rendering DB-sourced text.
const escapeHtml = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[c]));

const STOPPING_RULE_LABELS = {
  customer_opt_out: { name: "Opt-out honored", desc: "Customer opted out — no exceptions" },
  risk_flag: { name: "Risk case escalated", desc: "Never auto-retried a fraud/risk case" },
  max_attempts: { name: "Max attempts reached", desc: "Stopped after 3 automated attempts" },
  cooldown_24h: { name: "24h cool-off held", desc: "No stacked outreach within 24h" },
  candidates_exhausted: { name: "Ran out of options", desc: "No untried recovery actions left — routed to a human instead of giving up silently" },
  promise_pending: { name: "Promise pending held", desc: "Active promise-to-pay on file, not yet due — held instead of nagging" },
};

// Short one-line labels for the records table's Action column — the raw
// action names (e.g. "escalate_to_human" -> "Escalate To Human") wrapped
// across 3 lines at normal column widths, making rows inconsistent height
// and hard to scan. Full names are still visible via the title tooltip and
// in the record's audit-trail drill-down.
const ACTION_SHORT_LABELS = {
  retry_payment: "Retry",
  send_update_link: "Update Link",
  send_reminder: "Reminder",
  escalate_reminder: "Escalate Reminder",
  send_cart_recovery_link: "Recovery Link",
  send_discount_nudge: "Discount Nudge",
  escalate_to_human: "Escalate",
  stop_no_action: "Stop",
};

let currentFilter = "all";
let currentType = "all";
let currentBatchId = null;  // null = not yet resolved; set to a concrete batch_id once /api/batches loads
let allRecords = [];   // full filtered result from the API, kept in memory for client-side pagination
let currentPage = 1;
const RECORDS_PAGE_SIZE = 50;

function fmtBatchLabel(b) {
  const d = new Date(b.created_at);
  const when = isNaN(d) ? b.created_at : d.toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" });
  const c = b.record_counts || {};
  const total = (c.transactions || 0) + (c.receivables || 0) + (c.checkout_abandonments || 0);
  return `${when} · ${total} records`;
}

async function loadBatches() {
  const select = document.getElementById("batchSelect");
  try {
    const res = await fetch(`${API}/api/batches`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    if (!data.batches || data.batches.length === 0) {
      select.innerHTML = `<option value="">No batches yet</option>`;
      select.disabled = true;
      return;
    }
    currentBatchId = currentBatchId || data.current_batch_id;
    select.innerHTML = data.batches.map(b => `
      <option value="${escapeHtml(b.batch_id)}" ${b.batch_id === currentBatchId ? "selected" : ""}>
        ${b.batch_id === data.current_batch_id ? "● " : ""}${escapeHtml(fmtBatchLabel(b))}
      </option>
    `).join("");
    select.disabled = false;
  } catch (err) {
    console.error("Failed to load batches:", err);
    select.innerHTML = `<option value="">Couldn't load batches</option>`;
  }
}

document.getElementById("batchSelect").addEventListener("change", async (e) => {
  currentBatchId = e.target.value;
  await loadSummary();
  await loadRecords(currentFilter, currentType);
  await loadHeroExamples();
});

async function loadSummary() {
  try {
    const qs = currentBatchId ? `?batch_id=${encodeURIComponent(currentBatchId)}` : "";
    const res = await fetch(`${API}/api/summary${qs}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const s = await res.json();
    currentBatchId = s.batch_id;  // keep the dropdown and every subsequent fetch pinned to the batch actually shown

    document.getElementById("heroRecovered").textContent = fmtINR(s.total_recovered);
    document.getElementById("heroAtRisk").textContent = fmtINR(s.total_at_risk);
    document.getElementById("heroRate").textContent = s.recovery_rate_blended + "%";

    document.getElementById("txnRate").textContent = s.recovery_rate_transactions + "%";
    document.getElementById("txnAmounts").textContent = `${fmtINR(s.txn_recovered)} / ${fmtINR(s.txn_total)}`;
    document.getElementById("recvRate").textContent = s.recovery_rate_receivables + "%";
    document.getElementById("recvAmounts").textContent = `${fmtINR(s.recv_recovered)} / ${fmtINR(s.recv_total)}`;
    document.getElementById("abandRate").textContent = s.recovery_rate_abandonments + "%";
    document.getElementById("abandAmounts").textContent = `${fmtINR(s.aband_recovered)} / ${fmtINR(s.aband_total)}`;

    renderChips(s);
    renderFailureChart(s.by_failure_code);
    renderGuardrails(s.stopping_rule_breakdown, s.stopping_rule_triggers);
    loadBaselineComparison();
  } catch (err) {
    console.error("Failed to load summary:", err);
    document.getElementById("heroRecovered").textContent = "—";
    document.getElementById("chipRow").innerHTML = `<p class="loading-state error-state">Couldn't load summary data. Is the API running? <button onclick="loadSummary()" class="btn-ghost">Retry</button></p>`;
  }
}

async function loadBaselineComparison() {
  const el = document.getElementById("baselineCompare");
  try {
    const qs = currentBatchId ? `?batch_id=${encodeURIComponent(currentBatchId)}` : "";
    const res = await fetch(`${API}/api/baseline${qs}`);
    if (res.status === 400) {
      // Baseline comparison is only computed fresh for the current/latest
      // batch (see api.py's get_baseline_comparison) — not an error, just
      // not available for a historical batch someone's browsing via the
      // batch-history dropdown.
      el.innerHTML = `<p class="loading-state">Baseline comparison is only available for the latest batch — this is a historical run.</p>`;
      return;
    }
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const b = await res.json();
    const categories = [
      { key: "transactions", label: "Transactions" },
      { key: "receivables", label: "Receivables" },
      { key: "abandonments", label: "Abandonments" },
    ];

    el.innerHTML = categories.map(({ key, label }) => {
      const c = b[key];
      if (!c || !c.baseline_recovered) {
        return `<div class="baseline-secondary"><div class="baseline-secondary-row"><span>${label} — no policy-eligible records to compare.</span></div></div>`;
      }
      const barWidth = Math.min(100, (c.agent_recovered / c.baseline_recovered) * 100);
      const hasCi = c.recovery_rate_delta_ci_95 && c.recovery_rate_delta_ci_95[0] != null;
      const fmtPP = (v) => `${v >= 0 ? "+" : ""}${v}pp`;
      return `
        <div class="baseline-row">
          <div class="baseline-bar-group">
            <div class="baseline-bar-label">${label} · Baseline <span class="mono">${fmtINR(c.baseline_recovered)}</span></div>
            <div class="baseline-bar-track"><div class="baseline-bar-fill baseline-fill-dim" style="width:100%"></div></div>
          </div>
          <div class="baseline-bar-group">
            <div class="baseline-bar-label">${label} · AI Agent <span class="mono">${fmtINR(c.agent_recovered)}</span></div>
            <div class="baseline-bar-track"><div class="baseline-bar-fill baseline-fill-bright" style="width:${barWidth}%"></div></div>
          </div>
          <div class="baseline-incremental ${c.incremental >= 0 ? 'positive' : 'negative'}">
            ${c.incremental >= 0 ? "+" : ""}${fmtINR(c.incremental)} incremental
            (${c.incremental_pct >= 0 ? "+" : ""}${c.incremental_pct}%) · ${c.eligible_records} eligible of ${c.n_records}
          </div>
          ${hasCi ? `
          <div class="baseline-ci ${c.recovery_rate_delta_significant ? 'significant' : 'not-significant'}">
            Recovery rate: ${c.recovery_rate_agent}% agent vs ${c.recovery_rate_baseline}% baseline —
            ${fmtPP(c.recovery_rate_delta_pct_points)} (95% CI: ${fmtPP(c.recovery_rate_delta_ci_95[0])} to ${fmtPP(c.recovery_rate_delta_ci_95[1])})
            ${c.recovery_rate_delta_significant
              ? " — statistically significant at 95% confidence"
              : " — not statistically significant at this sample size (interval includes 0)"}
          </div>` : ""}
          ${c.caveat ? `<p class="baseline-caveat">${c.caveat}</p>` : ""}
        </div>
      `;
    }).join("");
  } catch (err) {
    console.error("Failed to load baseline comparison:", err);
    el.innerHTML = `<p class="loading-state error-state">Couldn't load baseline comparison. <button onclick="loadBaselineComparison()" class="btn-ghost">Retry</button></p>`;
  }
}

function renderChips(s) {
  // "Total at risk"/"Total recovered" used to duplicate the hero's ₹ figures
  // AND overflowed their box at narrow widths (fixed-size ₹ amounts don't
  // fit a 150px-min chip). Replaced with two genuinely new numbers — record
  // COUNTS, which appear nowhere else on the page (everything else is % or
  // ₹) — and every chip value now has overflow-safe sizing (see .chip-value
  // in styles.css) as a general fix, not just for these two.
  const chips = [
    { label: "Records processed", value: s.total_records, accent: "" },
    { label: "Records recovered", value: s.bucket_counts.recovered, accent: "green" },
    { label: "Human escalations", value: s.human_escalations, accent: "amber" },
    { label: "Stopping rules fired", value: s.stopping_rule_triggers, accent: "amber" },
    { label: "Flagged for manual follow-up", value: s.manual_followup_count, accent: "amber" },
    { label: "Re-planned after failure", value: s.replanned_count, accent: "blue" },
    { label: "In progress", value: s.bucket_counts.still_failing, accent: "" },
  ];
  document.getElementById("chipRow").innerHTML = chips.map(c => `
    <div class="chip ${c.accent ? 'accent-' + c.accent : ''}">
      <span class="chip-label">${c.label}</span>
      <span class="chip-value" title="${c.value}">${c.value}</span>
    </div>
  `).join("");
}

function renderFailureChart(byFailureCode) {
  const entries = Object.entries(byFailureCode).sort((a, b) => b[1].at_risk - a[1].at_risk);
  const el = document.getElementById("failureChart");
  el.innerHTML = entries.map(([code, stats]) => {
    const rate = stats.total ? (stats.recovered / stats.total * 100) : 0;
    return `
      <div class="bar-row">
        <div class="bar-row-top">
          <span class="bar-row-label">${escapeHtml(code.replace(/_/g, " "))}</span>
          <span class="bar-row-stat">${stats.recovered}/${stats.total} · ${rate.toFixed(1)}%</span>
        </div>
        <div class="bar-track"><div class="bar-fill" style="width:${rate}%"></div></div>
      </div>
    `;
  }).join("");
}

function renderGuardrails(breakdown, total) {
  document.getElementById("stoppingTotal").textContent = `${total} total`;
  const el = document.getElementById("guardrailList");
  const entries = Object.entries(breakdown);
  if (entries.length === 0) {
    el.innerHTML = `<p class="loading-state">No stopping rules fired this run.</p>`;
    return;
  }
  el.innerHTML = entries.map(([rule, count]) => {
    const meta = STOPPING_RULE_LABELS[rule] || { name: rule, desc: "" };
    return `
      <div class="guardrail-item">
        <div>
          <span class="guardrail-name">${meta.name}</span>
          <span class="guardrail-desc">${meta.desc}</span>
        </div>
        <span class="guardrail-count">${count}</span>
      </div>
    `;
  }).join("");
}

const BUCKET_LABELS = {
  recovered: "Recovered",
  escalated: "Escalated",
  still_failing: "In progress",
  stopped_no_action: "Stopped",
};

async function loadRecords(statusFilter = currentFilter, typeFilter = currentType) {
  currentFilter = statusFilter;
  currentType = typeFilter;
  const tbody = document.getElementById("recordsBody");
  tbody.innerHTML = `<tr><td colspan="6" class="loading-state">Loading records…</td></tr>`;

  const params = new URLSearchParams();
  if (statusFilter !== "all") params.set("status", statusFilter);
  if (typeFilter !== "all") params.set("record_type", typeFilter);
  if (currentBatchId) params.set("batch_id", currentBatchId);
  const qs = params.toString() ? `?${params.toString()}` : "";

  try {
    const res = await fetch(`${API}/api/records${qs}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    allRecords = await res.json();
  } catch (err) {
    console.error("Failed to load records:", err);
    tbody.innerHTML = `<tr><td colspan="6" class="loading-state error-state">Couldn't load records. <button onclick="loadRecords()" class="btn-ghost">Retry</button></td></tr>`;
    return;
  }

  // Filters run server-side against the full dataset (the fetch above already
  // reflects status/type filters) — pagination below only controls how much
  // of that already-filtered result renders into the DOM at once, so an
  // 800-row table doesn't become an 800-row page. Filter counts always match
  // allRecords.length, not just what's currently visible on-page.
  currentPage = 1;
  renderRecordsPage();
}

function renderRecordsPage() {
  const tbody = document.getElementById("recordsBody");
  const records = allRecords;

  if (records.length === 0) {
    tbody.innerHTML = `<tr><td colspan="6" class="loading-state">No records in this bucket.</td></tr>`;
    document.getElementById("recordsPagination").innerHTML = "";
    return;
  }

  const totalPages = Math.max(1, Math.ceil(records.length / RECORDS_PAGE_SIZE));
  currentPage = Math.min(currentPage, totalPages);
  const start = (currentPage - 1) * RECORDS_PAGE_SIZE;
  const pageRecords = records.slice(start, start + RECORDS_PAGE_SIZE);

  tbody.innerHTML = pageRecords.map(r => `
    <tr data-id="${escapeHtml(r.record_id)}">
      <td class="mono">${escapeHtml(r.record_id)}</td>
      <td style="text-transform:capitalize">${escapeHtml(r.record_type)}</td>
      <td>${escapeHtml(r.detail.replace(/_/g, " "))}${r.needs_manual_followup ? '<span class="followup-badge">manual review</span>' : ""}</td>
      <td class="mono">${fmtINR2(r.amount)}</td>
      <td style="text-transform:capitalize" title="${escapeHtml((r.action || "—").replace(/_/g, " "))}">${escapeHtml(ACTION_SHORT_LABELS[r.action] || (r.action || "—").replace(/_/g, " "))}${r.replanned ? '<span class="followup-badge" style="color:var(--blue);border-color:rgba(59,130,246,0.4)">re-planned</span>' : ""}</td>
      <td><span class="status-tag ${r.bucket}">${escapeHtml(BUCKET_LABELS[r.bucket] || r.bucket)}</span></td>
    </tr>
  `).join("");

  tbody.querySelectorAll("tr").forEach(tr => {
    tr.addEventListener("click", () => openReceipt(tr.dataset.id));
  });

  const paginationEl = document.getElementById("recordsPagination");
  if (totalPages <= 1) {
    paginationEl.innerHTML = "";
    return;
  }
  const rangeStart = start + 1;
  const rangeEnd = Math.min(start + RECORDS_PAGE_SIZE, records.length);
  paginationEl.innerHTML = `
    <span class="pagination-summary">${rangeStart}–${rangeEnd} of ${records.length}</span>
    <div class="pagination-controls">
      <button class="btn-ghost" id="pagePrev" ${currentPage === 1 ? "disabled" : ""}>Prev</button>
      <span class="pagination-page">Page ${currentPage} of ${totalPages}</span>
      <button class="btn-ghost" id="pageNext" ${currentPage === totalPages ? "disabled" : ""}>Next</button>
    </div>
  `;
  document.getElementById("pagePrev").addEventListener("click", () => { currentPage--; renderRecordsPage(); document.getElementById("recordsTable").scrollIntoView({ behavior: "smooth", block: "start" }); });
  document.getElementById("pageNext").addEventListener("click", () => { currentPage++; renderRecordsPage(); document.getElementById("recordsTable").scrollIntoView({ behavior: "smooth", block: "start" }); });
}

async function openReceipt(recordId) {
  const overlay = document.getElementById("receiptOverlay");
  const content = document.getElementById("receiptContent");
  content.innerHTML = `<p class="loading-state">Pulling the ledger…</p>`;
  overlay.classList.add("open");

  try {
    const res = await fetch(`${API}/api/audit/${encodeURIComponent(recordId)}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    content.innerHTML = renderReceipt(data);
  } catch (err) {
    console.error("Failed to load audit trail:", err);
    content.innerHTML = `<p class="loading-state error-state">Couldn't load this record's ledger. <button onclick="openReceipt('${recordId.replace(/'/g, "\\'")}')" class="btn-ghost">Retry</button></p>`;
  }
}

function renderCandidateTable(candidates, chosen) {
  const rows = candidates.map(c => `
    <div class="candidate-row ${c.candidate_action === chosen ? 'chosen' : ''}">
      <span class="candidate-name">${c.candidate_action === chosen ? "→ " : ""}${c.candidate_action.replace(/_/g, " ")}</span>
      <span class="candidate-prob">${(c.probability * 100).toFixed(0)}%</span>
      <span class="candidate-ev">₹${c.expected_value.toLocaleString("en-IN", {maximumFractionDigits: 0})}</span>
    </div>
  `).join("");
  return `<div class="candidate-table">
    <div class="candidate-row candidate-header">
      <span>ML candidate</span><span>P(recover)</span><span>Expected value</span>
    </div>
    ${rows}
  </div>`;
}

function renderReceipt(data) {
  const { record_type, record, diagnosis, decisions, audit_log } = data;
  const amount = record.amount;
  let label;
  if (record_type === "transaction") {
    label = record.failure_code.replace(/_/g, " ");
  } else if (record_type === "receivable") {
    label = `${record.days_overdue} days overdue`;
    // Phase 3: surface promise-to-pay state in the receipt, same string
    // shape as /api/records' detail field.
    if (record.promise_status === "broken" && record.promised_pay_date) {
      const daysBroken = Math.round((Date.now() - new Date(record.promised_pay_date)) / 86400000);
      label += ` · broken promise (was due ${daysBroken} day${daysBroken !== 1 ? "s" : ""} ago)`;
    } else if (record.promise_status === "pending" && record.promised_pay_date) {
      const daysUntil = Math.round((new Date(record.promised_pay_date) - Date.now()) / 86400000);
      label += ` · promise pending (due in ${daysUntil} day${daysUntil !== 1 ? "s" : ""})`;
    }
  } else {
    label = "cart abandoned" + (record.payment_attempted ? " (payment attempted)" : "");
  }

  // Pair each decision with the audit_log entries it produced. Every attempt
  // writes one ACTION_EXECUTED/RECOVERY_SUCCESS/ESCALATED/STOPPED entry, and
  // failed attempts (except the last) also write a REPLANNED entry right after.
  const outcomeEvents = audit_log.filter(a => a.event_type !== "REPLANNED");
  const attemptsHtml = decisions.map((dec, i) => {
    const outcome = outcomeEvents[i];
    const replanTag = dec.attempt_number > 1
      ? `<div class="replan-tag">↻ RE-PLANNED — attempt ${dec.attempt_number}, previous action didn't recover the payment</div>` : "";
    return `
      ${replanTag}
      <div class="receipt-step-label">${String(i + 2).padStart(2, "0")} · Decision (attempt ${dec.attempt_number})</div>
      <div class="receipt-step-body">${escapeHtml(dec.reasoning)}</div>
      ${dec.candidate_actions ? renderCandidateTable(JSON.parse(dec.candidate_actions), dec.ml_selected_action) : ""}
      ${dec.retry_at ? `<div class="receipt-meta"><span>Scheduled retry</span><span>${escapeHtml(dec.retry_at)}</span></div>` : ""}
      ${dec.stopping_rule_fired ? `<div class="receipt-meta"><span>Stopping rule</span><span>${escapeHtml(dec.stopping_rule_fired)}</span></div>` : ""}
      <div class="receipt-step-body" style="margin-top:6px;"><strong>Result:</strong> ${outcome ? escapeHtml(outcome.outcome) : "—"}</div>
      <hr class="receipt-divider">
    `;
  }).join("");

  return `
    <div class="receipt-title">Recovery Ledger</div>
    <div class="receipt-id">${escapeHtml(data.record_id)} &nbsp;·&nbsp; ${escapeHtml(record_type)}${decisions.length > 1 ? ` · ${decisions.length} attempts` : ""}</div>

    <div class="receipt-meta"><span>Customer</span><span>${escapeHtml(record.customer_id)}</span></div>
    <div class="receipt-meta"><span>Detail</span><span>${escapeHtml(label)}</span></div>
    <div class="receipt-total"><span>Amount</span><span>${fmtINR2(amount)}</span></div>

    <hr class="receipt-divider">

    <div class="receipt-step-label">01 · Diagnosis</div>
    <div class="receipt-step-body">${diagnosis ? escapeHtml(diagnosis.root_cause) : "—"}</div>
    ${diagnosis ? `<div class="receipt-meta"><span>Confidence</span><span>${diagnosis.confidence}</span></div>
    <div class="receipt-meta"><span>Risk flag</span><span>${diagnosis.risk_flag ? "true" : "false"}</span></div>
    ${record_type === "receivable" ? `<div class="receipt-meta"><span>Manual follow-up</span><span>${diagnosis.needs_manual_followup ? "flagged" : "no"}</span></div>` : ""}
    <div class="receipt-meta"><span>Urgency</span><span>${escapeHtml(diagnosis.recommended_urgency)}</span></div>` : ""}

    <hr class="receipt-divider">

    ${attemptsHtml}

    <div class="receipt-footer">— end of audit trail —<br>agent-generated · fully explainable</div>
  `;
}

document.getElementById("closeReceipt").addEventListener("click", () => {
  document.getElementById("receiptOverlay").classList.remove("open");
});
document.getElementById("receiptOverlay").addEventListener("click", (e) => {
  if (e.target.id === "receiptOverlay") e.target.classList.remove("open");
});

document.getElementById("filterRow").addEventListener("click", (e) => {
  if (!e.target.classList.contains("filter-btn")) return;
  document.querySelectorAll("#filterRow .filter-btn").forEach(b => b.classList.remove("active"));
  e.target.classList.add("active");
  loadRecords(e.target.dataset.filter, currentType);
});

document.getElementById("typeRow").addEventListener("click", (e) => {
  if (!e.target.classList.contains("filter-btn")) return;
  document.querySelectorAll("#typeRow .filter-btn").forEach(b => b.classList.remove("active"));
  e.target.classList.add("active");
  loadRecords(currentFilter, e.target.dataset.type);
});

document.getElementById("themeToggleBtn").addEventListener("click", () => {
  const current = document.documentElement.dataset.theme;
  const next = current === "light" ? "dark" : "light";
  document.documentElement.dataset.theme = next;
  localStorage.setItem("theme", next);
});

document.getElementById("runBatchBtn").addEventListener("click", async (e) => {
  const btn = e.target;
  btn.disabled = true;
  btn.textContent = "Running…";
  try {
    const res = await fetch(`${API}/api/run-batch`, { method: "POST" });
    if (!res.ok) {
      // A 409 (another run already in progress) or 429 (rate limited) or 500
      // (pipeline crash) all land here — previously this branch was never
      // taken because nothing checked res.ok, so a rejected run silently
      // rendered as if it had succeeded (see BUG_SWEEP_LOG.md pass 4).
      const errBody = await res.json().catch(() => null);
      throw new Error(errBody?.error?.message || `HTTP ${res.status}`);
    }
    const data = await res.json();
    currentBatchId = data.batch_id || null;  // pin the dashboard to the freshly-created batch
    await loadBatches();
    await loadSummary();
    await loadRecords(currentFilter);
    await loadHeroExamples();
    await loadEscalations();
    btn.textContent = "Re-run batch";
  } catch (err) {
    console.error("run-batch failed:", err.message);
    btn.textContent = "Failed — retry";
  }
  btn.disabled = false;
});

async function loadHeroExamples() {
  const qs = currentBatchId ? `?batch_id=${encodeURIComponent(currentBatchId)}` : "";
  const res = await fetch(`${API}/api/hero-examples${qs}`);
  const examples = await res.json();
  const el = document.getElementById("heroExamplesRow");
  if (examples.length === 0) { el.innerHTML = ""; return; }
  el.innerHTML = `<div class="hero-examples-label">Walk through a live example</div>
    <div class="hero-examples-cards">
    ${examples.map(e => `
      <button class="hero-example-card" data-id="${e.record_id}">
        <span class="hero-example-title">${e.label}</span>
        <span class="hero-example-blurb">${e.blurb}</span>
      </button>
    `).join("")}
    </div>`;
  el.querySelectorAll(".hero-example-card").forEach(btn => {
    btn.addEventListener("click", () => openReceipt(btn.dataset.id));
  });
}

function fmtAge(hours) {
  if (hours < 1) return "just now";
  if (hours < 24) return `${Math.round(hours)}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}

// Phase 4: Escalation Queue — a cross-batch operational worklist (not
// scoped to currentBatchId, matching /api/escalations' own default scope —
// a human works through this regardless of which batch created each item).
async function loadEscalations() {
  const el = document.getElementById("escalationsList");
  const countEl = document.getElementById("escalationsCount");
  try {
    const res = await fetch(`${API}/api/escalations`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const escalations = await res.json();
    countEl.textContent = `${escalations.length} open`;

    if (escalations.length === 0) {
      el.innerHTML = `<p class="escalations-empty">No open escalations — the agent is handling everything within policy.</p>`;
      return;
    }

    el.innerHTML = escalations.map(e => `
      <div class="escalation-item" data-escalation-id="${escapeHtml(e.escalation_id)}">
        <div class="escalation-main">
          <span class="escalation-record">${escapeHtml(e.record_id)} · ${escapeHtml(e.record_type)}</span>
          <span class="escalation-reason">${escapeHtml((e.reason || "").replace(/_/g, " "))}</span>
        </div>
        <div class="escalation-meta">
          <span class="escalation-amount">${e.amount != null ? fmtINR(e.amount) : "—"}</span>
          <span class="escalation-age">${fmtAge(e.age_hours)}</span>
          <button class="escalation-resolve-btn" data-escalation-id="${escapeHtml(e.escalation_id)}">Mark resolved</button>
        </div>
      </div>
    `).join("");

    el.querySelectorAll(".escalation-resolve-btn").forEach(btn => {
      btn.addEventListener("click", async () => {
        const id = btn.dataset.escalationId;
        btn.disabled = true;
        btn.textContent = "Resolving…";
        try {
          const res = await fetch(`${API}/api/escalations/${encodeURIComponent(id)}/resolve`, { method: "POST" });
          if (!res.ok) throw new Error(`HTTP ${res.status}`);
          // remove just this row from the open-queue view rather than a full reload
          const row = el.querySelector(`.escalation-item[data-escalation-id="${CSS.escape(id)}"]`);
          if (row) row.remove();
          const remaining = el.querySelectorAll(".escalation-item").length;
          countEl.textContent = `${remaining} open`;
          if (remaining === 0) {
            el.innerHTML = `<p class="escalations-empty">No open escalations — the agent is handling everything within policy.</p>`;
          }
        } catch (err) {
          btn.disabled = false;
          btn.textContent = "Failed — retry";
        }
      });
    });
  } catch (err) {
    console.error("Failed to load escalations:", err);
    el.innerHTML = `<p class="loading-state error-state">Couldn't load escalations. <button onclick="loadEscalations()" class="btn-ghost">Retry</button></p>`;
  }
}

(async function init() {
  await loadBatches();     // resolves currentBatchId to the latest batch before anything else fetches
  loadSummary();
  loadRecords();
  loadHeroExamples();
  loadEscalations();
})();
