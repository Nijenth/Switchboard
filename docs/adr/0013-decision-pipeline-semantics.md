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

- **Unknown data fails closed for tightening rules.** Conditions use three-valued logic: a
  missing field makes a leaf unknown, and `not`, `all` and `any` propagate unknown. A pack or
  tenant rule whose effect is `needs_approval` or `escalate` applies when its condition is
  unknown, and its reason records that required data was missing. A rule whose effect is `auto`
  or `ignore` applies only when its condition is definitely true.
- **The engine is pack-agnostic.** Any pinned, enabled pack that publishes rules and thresholds
  can be decided. Malformed rule content returns `escalate` with `INVALID_POLICY`.
- **The model may propose risk flags.** Classification output must include `risk_flags`, an
  array limited to `safety`, `harassment`, `legal_threat` and `chargeback`. Flags are stored on
  the model's classification row and can only tighten: any flag escalates through G1. The
  stated reason is kept so that extraction status and validity are derived as before.
- **Risk flags are sticky per case.** `/decide` reads flags from every classification on the
  case, so one flagged message keeps later decisions on that case at `escalate`.
- **Location.** Pure decision logic lives in `services/core/app/engine/`.

## Consequences

A rule written as "escalate unless X" can no longer be bypassed by omitting X. Rules keyed on a
classification can now apply when classification is missing; global rule G4 still wins those
ties, so the stated reason is the low-confidence rule. Escalation rules in other packs (for
example a bank-detail-change escalation) also fire when their data is missing, which may raise
more escalations until those packs mature. Enabling `/decide` for the minimal `sales_leads` and
`invoice_processing` packs is safe because nothing calls it for them yet and their decisions
default to human review. The AGENTS.md coverage gate of 90% for `app/engine` needs a coverage
tool, which is a new development dependency and is pending owner approval.
