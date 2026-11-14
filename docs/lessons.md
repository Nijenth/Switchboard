# Engineering lessons

## Ingress validation and idempotency

- The Stage 1 review confirmed that PostgreSQL cannot store NUL in text and that the API mapped the resulting data error to HTTP 503. Reject NUL before database access and classify permanent SQL errors separately from temporary connectivity and pool failures.
- The Stage 1 review confirmed that key-only deduplication returned the original response for changed content. Persist the normalized delivery digest with the original message. Treat a digest mismatch as HTTP 409 and preserve the original IDs on a matching retry.

## Mock outbound integration

- The pinned n8n CLI reported a successful publish while a subsequent sub-workflow call could not see a published version. The CLI updates the active version, while publication-service mode reads a separate publication record. Use consistent publication settings and verify a child call in the disposable harness.
- The grant replacement regression exposed an ambiguous `revision` reference that PostgreSQL resolved only when the function reached the affected statement. Use distinct names for PL/pgSQL variables and table columns, and exercise each authorization path through the restricted login.

## Order tool authorization

- The identity follow-up reproduced refund claims for a different order using the case-wide verification flag under migration 021's rules. Migration 022 checks message and order identity at both claim and consumption. Include a test for grants issued before an authorization change, since a claim check cannot govern grants already issued.

## n8n expressions

- The refund harness reproduced an expression syntax error when a nested object placed two closing braces next to each other inside an HTTP body expression. Keep the nested braces apart so they cannot be parsed as the expression terminator. Test the workflow in the pinned n8n runtime because the underlying JavaScript alone can parse successfully.

## Action state exits

- Approval expiry only handled pending requests. An approved request could leave an unclaimed action reserving its order indefinitely. Migration 026 gives the SLA watchdog a claim deadline and audits cancellation. Check the actual caller for every exit when a live state reserves a resource.
- Review promotion was split between the migration 012 Slack trigger and the API route. Migration 027 puts both channels through the same transaction and subject guards. Trace the request update and its triggers as well as the decision insert when reviewing approval transitions.
