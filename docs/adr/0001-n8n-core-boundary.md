# ADR 0001 — n8n orchestrates and executes; the core service decides and records

**Status:** Accepted
**Date:** 2026-11-13

## Context
The platform could be built as (a) a standalone automation engine that duplicates parts of n8n,
or (b) an n8n-based platform with a small core service. n8n already provides triggers, scheduling,
step retries, execution history and hundreds of connectors. Clients hiring for this work often ask
for n8n by name.

## Decision
Option (b). n8n owns triggers, sequencing, retries, connector calls (tool execution), Slack
interactions and schedules. The core service owns AI calls, guardrails, the policy engine, action
authorization (claim → grant → complete), and is the only writer of engine tables. n8n has no access
to engine tables. Its DB role can only reach the `demo` schema used by mock tools.

## Consequences
- We avoid rebuilding orchestration and connectors. Less code.
- Business-critical decisions are testable Python plus DB invariants, not visual IF nodes.
- n8n is replaceable for execution, but the platform intentionally depends on it.
- The claim/grant/complete protocol adds one round trip per action. This is accepted for crash safety.
- Tool contracts must be tested at the boundary: the n8n sub-workflow honors the grant, and the mock branch can never reach real credentials.
