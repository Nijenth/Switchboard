# ADR 0019 — Pack-declared lookups and identity verification

**Status:** Accepted for the offline mock adapter

**Date:** 2026-11-14

## Context

Order-status replies and refund decisions need facts from business systems, such as an order's
status. `order_lookup` already requires a verified identity at claim time, but nothing verified
a case's identity, nothing proposed read tools, and `/decide` always saw empty `checks.*`.

## Decision

- **Packs declare lookups.** `pack.yaml` lists read tools in order, with each input mapped from a
  validated extraction field or `message.sender_address`. The pack validator requires selected
  read tools with registry input and output schemas, inputs that match the input schema, known
  extraction fields, and output field names that do not collide across lookups.
- **Core proposes, workers execute.** `POST /lookups` (its own scoped credential) creates missing
  lookup actions as `proposed` read actions with the key `lookup:<message>:<tool>` and reports
  `pending`, `completed` and `waiting`. Callers run pending actions through the existing claim,
  consume and complete protocol, then call again until nothing is pending. A tool that requires
  a verified identity is proposed only after the case is verified, and the database still
  refuses its claim otherwise.
- **Read results are validated twice.** `POST /actions/{id}/complete` accepts `output` for read
  actions. Core validates it against the tool's registry output schema without remote
  references, and `complete_mock_read` stores it only for a consumed, unexpired grant.
- **Identity comes from a dedicated check.** `order_identity_check` returns only `order_found`
  and `identity_verified`, never order data. The database marks the case verified, with an audit
  event, only when a completed read tool whose registry output schema declares
  `identity_verified` returns `true`.
- **Results become checks.** `/decide` merges outputs of executed read actions for the message
  into `checks.*` and records them in the decision inputs.

## Consequences

Order data can reach rules only after the sender's email matched the order. Email sender
addresses can be spoofed, so real adapters must require the provider's SPF/DKIM pass before an
email-based identity check counts; the mock adapter trusts the fixture. A lookup that fails or
ends in an unknown outcome stays reported as waiting, because its idempotency key is fixed;
retry policy for read tools is pending. The n8n tool workflows for the two lookups and the
pipeline loop are the next slice. Drafts cannot yet cite tool results.
