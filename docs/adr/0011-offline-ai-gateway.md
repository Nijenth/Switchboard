# ADR 0011 — Offline AI development through LiteLLM

**Status:** Accepted for offline development. Live model IDs and spend caps require approval.

**Date:** 2026-11-14

Core calls LiteLLM through two stable aliases, `switchboard-openai` and `switchboard-anthropic`.
On a retryable failure or invalid structured output, core selects the other alias once. Each
attempt has its own budget admission and audit record. Gateway retries are disabled so a request
cannot trigger hidden provider calls. There is no drafting alias in Phase 2.

Local runs and CI use the offline Compose profile. The fake server offers OpenAI Chat Completions
and Anthropic Messages protocol fixtures on an internal network. It supports valid schema-shaped
output, malformed output, timeouts, and upstream errors. Fixture scores verify application
behavior. Phase 2 is **built, not measured** until an explicitly authorized real-model eval.

Future live deployments read `OPENAI_API_KEY` and `ANTHROPIC_API_KEY` from local `.env`.
`SWITCHBOARD_OPENAI_MODEL` and `SWITCHBOARD_ANTHROPIC_MODEL` are intentionally unset pending
confirmation. Keep provider credentials in the gateway. Enable real calls only after the owner
confirms both keys and spending caps. Langfuse is off by default. Any future integration may
send redacted text only and must never attach raw inputs or model responses.

Candidates for a later real eval are `gpt-6-luna` with reasoning disabled and
`claude-haiku-5-5` with low effort. Model settings live in LiteLLM. Proposed AGENTS.md
rule: use temperature ≤ 0.2 where the provider supports it. Otherwise use structured output,
low reasoning effort, and eval-measured consistency. Run each case twice and report agreement.
[OpenAI model documentation](https://developers.openai.com/api/docs/models/gpt-6-luna).
[Anthropic model documentation](https://platform.claude.com/docs/en/models/haiku-5-5/overview).

Core applies the existing tenant monthly budget in UTC before each attempt. Keep the maximum
billable request cost reserved before sending the request. A lost or ambiguous response keeps
the reservation charged against admission until reconciled. Budget admission is serialized per
tenant and does not hold a transaction open over a network call.

Core reuses the pinned `jsonschema` dependency from pack validation to validate extraction
values independently of the gateway. It sends only the stored redacted body as a JSON string
inside customer-message delimiters. Real data requires stronger redaction before any live run.
