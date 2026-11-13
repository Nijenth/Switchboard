# ADR 0009 — Run the mock outbound loop through n8n

**Status:** Accepted

**Date:** 2026-11-13

n8n posts approval requests, runs the mock connector, and schedules reconciliation and checkpointing. Core supplies scoped approval metadata, records Slack delivery, and decides authorization. n8n stores mock send receipts in `demo` using its mock login. That login has no engine access. The reconciler obtains pending attempts from core and looks up receipts by tenant and action.

The fixed lease still applies. Core can replace an unused grant after a policy revision changes by revalidating current authority. Consumed grants and unknown outcomes go through reconciliation. Confirmed absence requires a 30-second grace period after expiry. The audit schedule calls a separate checkpoint process which owns the checkpoint history and has no database credentials.
