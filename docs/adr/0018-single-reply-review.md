# ADR 0018 — One review for an exact mock reply

**Status:** Accepted for the offline reply workflow

**Date:** 2026-11-14

The Slack reviewer approves the exact outbound draft, including its recipient, channel,
body, citations, and checks. Core queues one mock send per draft and requests approval only
for the draft. The database may claim that send directly from proposed once the latest
human approval and the exact draft policy binding are current.

This authority applies only to the canonical mock send whose sole input is the draft ID,
whose message matches the draft, and whose destination matches trusted tenant configuration.
Claim and grant consumption retain their existing locks, payload hashes, and policy
revision checks. A later revision invalidates the draft-only send until reviewed again.
Existing action approvals retain their existing path. Money actions require their own
approval. The real connector branch is outside this ADR.
