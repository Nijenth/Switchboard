# ADR 0016 — Bind offline draft review to the exact payload

**Status:** Accepted for offline drafting

**Date:** 2026-11-14

Core retrieves evidence within the authenticated tenant and pinned pack version. The fake
LiteLLM alias receives redacted customer text and knowledge in separate untrusted-data blocks.
The API takes message and case IDs. Citation IDs must come from the retrieved set. The
current evidence source is pack knowledge. Tool-result citations require a verified result
ingestion path and can be added with the tool workflow.

For automatic handling, the reply must match a complete sentence from its cited evidence
and pass local checks for detected PII, unsupported promises, injection, and hostile tone.
These checks are deliberately limited to the synthetic offline slice. Core evaluates policy
and persists the decision against the database-computed draft hash in the same transaction
as the draft. PostgreSQL requires a nonempty citation array and both guardrail passes on the
matching payload for automatic reply authorization. The existing human approval path
retains its own exact-payload binding. The API does not create or dispatch a send action.

The existing live-draft index prevents duplicate live rows. A concurrent draft request can
make a redundant fixture call before final persistence returns the existing draft.
