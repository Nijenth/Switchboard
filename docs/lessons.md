# Engineering lessons

## Ingress validation and idempotency

- The Stage 1 review confirmed that PostgreSQL cannot store NUL in text and that the API mapped the resulting data error to HTTP 503. Reject NUL before database access and classify permanent SQL errors separately from temporary connectivity and pool failures.
- The Stage 1 review confirmed that key-only deduplication returned the original response for changed content. Persist the normalized delivery digest with the original message. Treat a digest mismatch as HTTP 409 and preserve the original IDs on a matching retry.

## Mock outbound integration

- The pinned n8n CLI reported a successful publish while a subsequent sub-workflow call could not see a published version. The CLI updates the active version, while publication-service mode reads a separate publication record. Use consistent publication settings and verify a child call in the disposable harness.
- The grant replacement regression exposed an ambiguous `revision` reference that PostgreSQL resolved only when the function reached the affected statement. Use distinct names for PL/pgSQL variables and table columns, and exercise each authorization path through the restricted login.
