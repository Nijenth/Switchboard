# ADR 0013 — Decision pipeline semantics

**Status:** Accepted

**Date:** 2026-11-14

## Context

`/decide` evaluates global safety rules, pack rules and tenant tightening rules over a decision
context built from stored classifications, extractions and case data. The first review found
three gaps: a tightening rule silently did not apply when the data it checked was missing, the
endpoint refused every pack except `ecommerce_support`, and G1 could only see flags from local
pattern checks, so `safety` and `harassment` were never raised.

## Decision

- **Unknown data fails closed for tightening rules.** Missing and null fields behave alike.
  `exists` treats both as absent. Comparisons can return unknown, which propagates through
  `not`, `all`, and `any` unless another operand determines the result. Unknown qualifies
  `needs_approval` and `escalate` rules. `auto` and `ignore` require a definite match.
- **Strictest pack match wins.** Escalation outranks approval, and both outrank `auto` and
  `ignore`. File order breaks ties within each rank. Global guards and tenant rules can
  tighten the selected result. The winning reason includes a missing-data annotation when
  that rule matched on unknown.
- **The engine is pack-agnostic.** It validates the policy inputs it consumes, including
  thresholds, rule lists, rule structure, and extraction schema when configured. An invalid
  policy yields urgent `escalate` with `INVALID_POLICY` and the `admin` role. `/decide`
  persists that decision for an eligible case and its latest inbound message. Case
  eligibility, credential errors, and database failures retain their normal API responses.
  Extraction validation uses the existing JSON Schema reference registry with no remote
  retrieval.
- **The model may propose risk flags.** Classification output must include `risk_flags`, an
  array limited to `safety`, `harassment`, `legal_threat` and `chargeback`. Flags are stored on
  the model's classification row and can only tighten: any flag escalates through G1. The
  stated reason is kept so that extraction status and validity are derived as before.
- **Risk flags are sticky per case.** `/decide` reads flags from every classification on the
  case, so one flagged message keeps later decisions on that case at `escalate`.
- **Location.** Pure decision logic lives in `services/core/app/engine/`.

## Consequences

A stricter pack rule can supersede an earlier rule even when it matched on missing data.
Global guards win ties against pack decisions. An escalation triggered by missing data
outranks G4. Existing decision records keep their policy version. New publications use
`engine:2`. CI enforces the engine coverage threshold under ADR 0015.
