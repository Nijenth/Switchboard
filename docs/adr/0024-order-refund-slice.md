# ADR 0024 — Approve and execute mock order refunds

**Status:** Accepted for the offline mock adapter

## Decision

- Publish ecommerce support v2 next to v1. V2 selects new eligibility and refund tool
  contracts because released registry entries are immutable. The demo seed selects v2.
  Existing cases retain their pinned pack.
- The scoped `POST /refunds/propose` endpoint accepts a case and inbound message ID.
  The pack config identifies the refund intent and money tool. Core evaluates policy
  and creates the pending action and approval request in one transaction. Only executed
  eligibility evidence supplies the amount and currency. The approval trigger derives
  the reviewer role and SLA from the pack and tenant configuration.
- Migration 024 identifies eligibility tools by their registry schemas. Order-scoped
  money actions require matching eligibility at proposal, approval, claim, replacement,
  and dispatch. A per-order transaction lock serializes evidence publication with
  authorization. The unique index reserves the tenant/order through execution and any
  unknown outcome. A rejection, cancellation, or confirmed failure releases it.
- The mock adapter locks the order and atomically records a receipt in
  `demo.order_refunds` and marks the order refunded. A repeat with the same idempotency
  key returns the receipt. The reconciler searches by that key and submits the stored
  result to core. The database checks the result schema at completion and reconciliation.

## Deployment

Run migration 024 and publish the catalog before enabling the new workflow exports.
Bind the proposal node to a separate tenant-scoped Header Auth credential. For the
demo, provision fresh fixtures through `scripts/seed`. The workflow executes only the
mock adapter. A real payment integration will need its own idempotency and reconciliation
contract.
