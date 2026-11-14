# ADR 0021 — Order- and message-scoped identity verification and lookup recovery

**Status:** Accepted for the offline mock adapter

**Date:** 2026-11-14

## Context

ADR 0019 stored identity verification as a boolean on `public.cases` (`cases.identity_verified`).
In a multi-message thread on the same case, a sender who verified order `NW-1001` in message 1
left `cases.identity_verified = true` for later messages on that case, allowing a later message
asking about another customer's order (`NW-1002`) to propose and claim `order_lookup` or satisfy
the `ORDER_STATUS` automatic rule. In addition, `app_core` retained direct `INSERT`/`UPDATE`
privileges on `cases.identity_verified` from migration 004, `order_lookup` carrier and tracking
fields lacked strict patterns (causing 15-digit numeric tracking numbers such as `NW-1004`'s
`771200000000004` to be redacted as `[CARD]`), and an `executing` read lookup whose lease expired
or whose mock query returned zero rows remained stuck.

## Decision

- **Per-message, per-order database enforcement.** `public.order_identity_is_verified` requires
  `cases.identity_verified` to be `true`, an `executed` identity-check read action on the **same
  message** and **same `order_number`** with `order_found = true` and `identity_verified = true`,
  and no executed identity check on the message (or at/after that check on the case) that failed.
  `enforce_action_rules()`, `consume_mock_grant()`, and `replace_unused_mock_grant()` enforce
  `public.order_identity_is_verified` for `order_lookup`.
- **Failed checks clear reachability; only `complete_mock_read` mutates `cases.identity_verified`.**
  Migration 021 revokes `INSERT (identity_verified), UPDATE (identity_verified)` on `public.cases`
  from `app_core`. `complete_mock_read` rejects `order_found = false` with
  `identity_verified = true`, sets `cases.identity_verified = false` on any failed identity check,
  and `complete_mock_action` refuses `read` actions without an output payload. `/lookups`,
  `/decide`, and `/drafts` (`lookup_evidence`) all gate `order_lookup` and
  `context["case"]["identity_verified"]` on `public.order_identity_is_verified`.
- **Strict schema for `order_lookup` and verbatim tool tokens.** `carrier` (`^[A-Za-z]{2,20}$`)
  and `tracking_number` (`^[A-Z0-9]{6,32}$`) use strict patterns in `packs/tool_registry.yaml`.
  `render_evidence` quotes values that validate against strict schema constraints (`enum`,
  `pattern`, `format`, `const`) without redacting numeric tracking numbers into `[CARD]`, while
  `assess` allows uppercase alphanumeric tokens (`[A-Z0-9]{6,32}`) only when present in a cited
  `tool:` evidence sentence.
- **Recoverable read lookups.** `claim_mock_action` and `enforce_action_rules()` allow reclaiming
  an `executing` `read` action after its lease expires (`attempts + 1`), `wf_tool_order_lookup`
  and `wf_tool_order_identity_check` set `alwaysOutputData: true` so empty mock queries still
  complete, and `wf_lookups_run` exits after round 1 when nothing is pending.

## Consequences

Verifying one order on a thread never authorizes `order_lookup` or automatic replies for a
different order or a later message, and any failed identity check immediately revokes case-level
and order-level reachability. Read actions are safe to re-claim after lease expiry because they
have no external side effects.
