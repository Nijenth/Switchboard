# ADR 0003 — Bind core ingress to an endpoint credential

**Status:** Accepted for Stage 1

**Date:** 2026-11-13

## Context

Public webhooks and the private core API have different callers. The public edge must verify the sender's signature over the original request. Core receives normalized input from a trusted ingress adapter and must establish which endpoint that adapter may use before setting database tenant context.

## Decision

- Public webhook adapters verify HMAC and a five-minute timestamp window before normalization. Signing secrets live in the adapter's credential store, or an injected secret in local development. The public webhook contract lands with its adapter.
- The private `/ingest` API accepts a separate, random 256-bit bearer token per endpoint over a protected transport. The caller stores the plaintext in its credential store. PostgreSQL stores only the SHA-256 digest, the bound endpoint, and the credential's expiry and revocation metadata. Use distinct credentials for ingress and health checks.
- The configuration loader provisions and revokes credential digests. Core has no table access to the digests. A `SECURITY DEFINER` function callable by `app_core` resolves a digest to only the tenant, endpoint, and current pack identifiers plus the endpoint channel and address. Unknown, expired, revoked, and disabled bindings yield no row. Core compares any claimed tenant, channel, and address with that result before opening tenant-scoped work in the same transaction.
- Disabling an enrollment blocks new routing to it and new ingress through its mapped endpoints, including replies to existing cases. Existing cases keep their pinned version and remain available for staff review and manual resolution. Stage 1 has no automated outbound execution. Future claim policy must explicitly check whether the enrollment is still enabled.

## Consequences

The database can resolve ingress without storing a usable bearer secret. Rotation can overlap two digests for the same endpoint and revoke the old digest after the caller switches. TLS and restricted access to the private API remain deployment requirements. Credential values, digests, and request bodies are excluded from application logs. Endpoint-scoped message idempotency handles duplicate deliveries.
