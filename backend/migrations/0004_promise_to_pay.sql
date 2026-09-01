-- Phase 3 of the feature-completion loop: turns the "promise-to-pay
-- outreach" LABEL that already existed in diagnosis.py's 45+ day overdue
-- tier text into a real, tracked feature. Before this, there was no field
-- recording that a customer actually promised a payment date, and no
-- distinction between "never promised" / "promised and pending" /
-- "promised and broken."
--
-- promise_status: 'none' (no promise on file, the default) / 'pending'
-- (promised date hasn't arrived yet — hold, don't nag) / 'broken' (promised
-- date passed with the invoice still unpaid — escalate immediately,
-- regardless of days_overdue tier). See diagnosis.py::diagnose_receivable
-- and candidate_actions.py::candidates_for_receivable for how this changes
-- diagnosis and candidate-action eligibility.
ALTER TABLE receivables ADD COLUMN promised_pay_date TEXT;
ALTER TABLE receivables ADD COLUMN promise_status TEXT NOT NULL DEFAULT 'none';
