# ADR 0009 — Run the mock outbound loop through n8n

**Status:** Accepted

**Date:** 2026-11-13

n8n posts approval requests, runs the mock connector, and schedules reconciliation and checkpointing. Core supplies scoped approval metadata, records Slack delivery, and decides authorization. n8n stores mock send receipts in `demo` using its mock login. That login has no engine access. The reconciler obtains pending attempts from core and looks up receipts by tenant and action.

The fixed lease still applies. Core can replace an unused grant after a policy revision changes by revalidating current authority. Consumed grants and unknown outcomes go through reconciliation. Confirmed absence requires a 30-second grace period after expiry. The audit schedule calls a separate checkpoint process which owns the checkpoint history and has no database credentials.

## Verification and failure handling

The disposable harness imports the same workflow nodes with manual test triggers in place of schedules. It uses fake Slack, a scratch n8n database, a restricted mock database login, and a separate checkpoint process. For the pinned n8n CLI, publication service mode must match the CLI publication path. When it does not, a successful publish command can still leave the child workflow unavailable at execution time.

Mock sends write an idempotent receipt before reporting completion. A missing completion leaves that receipt available to the reconciler. The approval workflow records Slack's channel and timestamp before the signed callback can authorize the subject. An ambiguous Slack delivery can leave a duplicate notification, but only the recorded message can be used for review.
