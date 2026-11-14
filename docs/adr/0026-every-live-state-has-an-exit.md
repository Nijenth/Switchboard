# ADR 0026 — Every live state has an exit

Every non-final action state must have a time-bound automatic exit or a
documented human exit with a real caller. Review the caller whenever a state
can reserve an order or block replacement work.

An approval can outlive its usefulness before a worker claims the action.
Start the claim window at the approval request's recorded resolution time.
Read `tenant_settings.approval_claim_window_minutes` on each check. The default
is 60 minutes. Core refuses late claims and the SLA watchdog cancels overdue,
unclaimed actions, preserving the approval history.

| Action state | Exit | Caller |
|---|---|---|
| `proposed` | Claim authorized work. If a review is attached, reject, replace or expire it as below. An abandoned proposal without a review still has no cleanup caller. | Worker via `POST /actions/{id}/claim`, or the reviewer and watchdog for attached reviews. |
| `pending_approval`, request pending | Reviewer approves, rejects or replaces the action. An overdue request cancels the action. | Reviewer via `POST /approvals/slack` or `POST /approvals/{id}/decide`. The SLA watchdog calls `POST /actions/approvals/expire`. |
| `pending_approval` or `approved`, request approved | Claim eligible work inside the window. Cancel unclaimed work after the window. | Worker via `POST /actions/{id}/claim` for promoted actions. The SLA watchdog calls `POST /actions/approvals/expire-unclaimed` for either shape. |
| `executing` | Complete with a receipt, or reconcile after the 45-second lease. Expired read leases can be reclaimed. | Worker via `POST /actions/{id}/complete` or `/claim` for reads. `wf_action_reconciler` calls `POST /actions/{id}/reconcile` for mock writes. |
| `unknown_outcome` | Confirmed receipt means `executed`. Confirmed absence after the 30-second grace period means `failed`. Inconclusive evidence is a deliberate human-only hold. | The reconciler records conclusive lookup results. If the lookup is inconclusive, an operator checks the adapter ledger by idempotency key, obtains a stable lookup reference, and submits conclusive evidence through `POST /actions/{id}/reconcile` with the reconciler credential. Keep the hold while the evidence remains inconclusive. |

`failed`, `rejected` and `cancelled` release the order reservation.
`executed` retains it to prevent a second payout.
