# ADR 0022 — Bind order tool authority to the inbound sender and order

**Status:** Accepted for the offline mock adapter

**Date:** 2026-11-14

## Context

Migration 021 scoped `order_lookup` to a message and order. The database still accepted
identity actions with a caller-supplied sender, trusted core's validation of lookup results,
and accepted the case-wide identity flag alone for refund actions.

## Decision

- Compare an identity check's `sender_email` with the inbound message's sender in the
  same tenant and case. Use the same case-insensitive comparison as the mock shop.
  Validate the binding when an action is created, edited, claimed, dispatched, or completed.
  Earlier positive results must also pass the binding check to authorize an order.
- Require identity for the same message and order at claim, grant replacement, and
  consumption for identity-gated tools that declare or receive an `order_number`. Treat
  `refund_eligibility_check` and `refund_issue` as order tools even when the input omits
  `order_number`, since their released registry entries have no input schema. Refund
  approvals still apply.
- Validate executed `order_lookup` outputs using `tools.output_schema`, the database copy
  of `packs/tool_registry.yaml`. PostgreSQL reads the allowed fields, required fields,
  types, enum, lengths, and patterns from the registry. It checks the calendar day and
  timezone-bearing timestamp when the registry declares `date-time`. A change that
  introduces unsupported schema keywords needs a corresponding migration.

## Rollout

Apply migration 022 before deploying the core changes. The migration preserves action history.
Core excludes earlier lookup results that fail current validation from policy inputs and draft
evidence. A worker with a grant that no longer passes identity checks receives HTTP 409.
A malformed lookup result returns HTTP 422 and the worker may submit a corrected result
using the same consumed grant before it expires.
