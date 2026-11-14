# ADR 0027 — Promote approved subjects in PostgreSQL

Migration 012 already promoted Slack-approved actions and drafts after resolving
their approval requests. API reviews promoted subjects with a second update in
core. Migration 027 extends the existing database trigger to all review channels
and removes the duplicate API updates. Draft reviews use the same trigger and
retain their existing authorization checks.

The trigger runs after the request becomes approved, when the recorded decision
and resolved request are both visible to the subject guards. It matches the tenant,
case, subject ID and reviewed payload hash. For actions it uses the recorded
reviewer as the approver. Existing state, policy and eligibility checks still
apply. A refusal rolls back the decision and leaves the request pending for SLA
expiry. Migration 026 continues to enforce the claim deadline.
