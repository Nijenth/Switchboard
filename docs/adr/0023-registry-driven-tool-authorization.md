# ADR 0023 — Derive tool authorization from registry contracts

**Status:** Accepted for the offline mock adapter

**Date:** 2026-11-14

## Decision

- An active read tool can establish or revoke order identity when its output schema declares
  `identity_verified` and its input schema requires `order_number` and `sender_email`.
  PostgreSQL uses this definition to verify sender binding, record the identity outcome,
  and select evidence for the same message and order.
- An identity-gated tool must declare `order_number` as required. PostgreSQL blocks tools
  missing that declaration from entering pending approval or approval, claiming execution,
  replacing a grant, or consuming a grant. Order-scoped tools require current identity
  for the action's message and order.
- PostgreSQL validates results from identity-gated tools against each tool's own output
  schema when recording execution. The validator supports the existing flat object contract
  and rejects unsupported schema keywords. Core applies the same checks before completed
  reads can supply decision inputs or draft evidence.

## Rollout

Coordinate migration 023 with the core deployment because it replaces the earlier database
validation functions. Existing grants are rechecked at dispatch. The released
`refund_eligibility_check` and `refund_issue` contracts lack a required `order_number`.
The refund slice will publish new tool keys with that requirement because pack sync treats
released tool contracts as immutable. The current mock completion API accepts output for
read actions. A future refund execution path must record its validated output as part of
completion.
