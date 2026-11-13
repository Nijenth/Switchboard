# Engineering lessons

## Ingress validation and idempotency

- The Stage 1 review confirmed that PostgreSQL cannot store NUL in text and that the API mapped the resulting data error to HTTP 503. Reject NUL before database access and classify permanent SQL errors separately from temporary connectivity and pool failures.
- The Stage 1 review confirmed that key-only deduplication returned the original response for changed content. Persist the normalized delivery digest with the original message. Treat a digest mismatch as HTTP 409 and preserve the original IDs on a matching retry.
