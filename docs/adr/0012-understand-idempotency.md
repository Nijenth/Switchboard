# ADR 0012 — Persist understanding claims and guardrail results

**Status:** Accepted

A request claims a message and its pinned pack ID before the gateway call. The default request
has a stable key. An explicit rerun supplies a fresh UUID. The same key replays the completed
response or returns HTTP 409 while the claim has no result. An interrupted claim stays reserved
because the provider may have billed it. An operator can explicitly request a fresh rerun.
Classification, extraction, audit entry, and result commit together. The claim and each gateway
attempt commit separately, so network calls do not hold database transactions open.

Local guardrails record a rules classification with the reserved `system_escalation` intent
and a stable reason in `classifications.flags`. The reserved intent is outside the model's
allowed intent list. This gives Phase 3 a durable escalation signal even when no model ran.
