# Demo Narration Script (~60 seconds)

Timed for spoken delivery — read it out loud once before presenting; scripts that read fine on paper often run long spoken.

---

**[0:00-0:10] The problem**

"Companies lose revenue silently — a card expires, a bank times out, an invoice sits unpaid for two months. Most of it's recoverable, if you diagnose *why* it failed and pick the right fix instead of blindly retrying everything the same way."

**[0:10-0:20] What it does — click "Re-run batch"**

"This is Reclaim — an AI revenue recovery agent. Watch — I'll run it live." *[click Re-run batch]* "It just diagnosed 800 failed payments, overdue invoices, and abandoned checkouts — all three revenue-risk categories — scored multiple recovery actions with a trained ML model, picked the best one by expected value, executed it, and — if that failed — re-planned and tried something else. All bounded, never more than 3 attempts, ever."

**[0:20-0:32] The headline number**

"Look at the recovered total against what's at risk." *[point to hero number]* "But the number that actually matters is this—" *[point to Baseline vs Agent panel]* "—against a naive 'always retry once' baseline, with a 95% confidence interval on every category, not just a point estimate. Transactions come back statistically significant. Receivables and abandonments are reported honestly as not-yet-significant at this sample size — we show that too, not just the wins."

**[0:32-0:42] The compliance proof point**

"And it's not spamming anyone to get there." *[point to Stopping Rules panel]* "Every time a guardrail fires, it's logged — opt-outs honored, risk cases never auto-retried, nobody gets more than 3 automated attempts. And every time the agent escalates to a human instead of guessing, it lands as a real, resolvable ticket—" *[point to Escalation Queue]* "—not just a status string nobody can act on."

**[0:42-0:55] Live click-through**

*[click "Re-plan then recover" hero card]* "Here's one transaction end to end. Diagnosis: insufficient funds. The agent compared two candidate actions — retry now at 26%, retry later at 53% — picked the smarter one, it failed anyway, re-planned—" *[scroll to show the RE-PLANNED marker]* "—tried the other option, and recovered the money. Every step, every probability, every reason, logged."

**[0:55-1:00] Close**

"We don't just identify revenue leakage. We recover it — measurably, safely, and with a complete audit trail."

---

## Notes for the presenter

- The exact ₹ numbers move a little between runs (each "Re-run batch" mints a fresh, additively-generated batch — it does NOT erase the batch you just showed; the batch-history dropdown in the header lets you flip back to it if you need to). Don't memorize exact figures, just gesture at the panels.
- The Baseline vs. Agent panel now shows a 95% CI per category, not just a headline %. Don't claim "the agent beats baseline everywhere" — say what the panel actually says (significant vs. not-yet-significant at current sample size). That honesty is a feature, not a hedge — lean into it if asked a hard question.
- If the "Re-plan then recover" hero card isn't available on a given run (rare), any of the three hero cards works — all three map directly to a beat in this script.
- Have the dashboard already loaded before starting the timer; don't demo the page load itself.
