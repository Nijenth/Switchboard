# ADR 0020 — Drafts that cite tool results

**Status:** Accepted for the offline mock adapter

**Date:** 2026-11-14

## Context

ADR 0019 made verified order data available to policy as `checks.*`, but drafts could cite only
knowledge chunks. An order-status reply therefore could not state the order's status, and the
drafting guardrail rejected any order number in a reply.

## Decision

- **Packs own the wording.** A lookup entry in `pack.yaml` may declare an `evidence` sentence
  template (at most 300 characters) with `{field}` placeholders. The validator requires each
  placeholder to be a declared lookup input or a registry output field. Placeholders mapped
  from `message.*` are refused, so evidence never repeats unverified customer input.
- **Core renders evidence from stored results only.** `/drafts` renders the template for each
  executed read action of the message, using its stored input and validated output. A missing,
  null, or structured value yields no evidence, never a partial sentence. Rendered sentences pass
  through the same PII redaction as knowledge before reaching the model. Each sentence gets the
  ID `tool:<action id>` and comes before knowledge chunks in the untrusted evidence block.
- **Same grounding rule as knowledge.** Automatic handling still requires the reply to be an
  exact sentence of every cited source. A tool citation is stored as
  `{"type": "tool", "action_id": ...}`.
- **Order references need a cited tool result.** The privacy check now allows an order number
  only when it exactly matches one in a cited tool-result sentence. Knowledge chunks cannot
  vouch for order numbers, and other redaction placeholders remain refused.
- **Evidence is rechecked before storing.** The second transaction re-renders the tool evidence
  and refuses with 409 if it changed while the model ran, as it already does for knowledge.

## Consequences

Order-status replies from a verified sender can be handled automatically by `ORDER_STATUS`
with an exact quote of the lookup. A real adapter's output must still match the registry output
schema before it can be quoted. Tracking numbers and carrier names are quoted as returned, so
real adapters must not return customer PII in those fields. The drafting quality status stays
**built, not measured**.
