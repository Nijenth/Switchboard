# ADR 0014 — Deterministic offline knowledge search

**Status:** Accepted for the offline ecommerce slice

**Date:** 2026-11-14

Use PostgreSQL to encode pack chunks and queries with the same 1536-dimensional fixture
encoder. It lowercases ASCII tokens, removes common English stopwords, and hashes distinct
tokens with SHA-256 into binary vector slots. pgvector ranks scoped chunks by exact cosine
similarity, with chunk ID as the tie-breaker. Empty vectors produce no matches.

The pack loader embeds new pack knowledge in the same transaction as publication. Migration
017 backfills previously loaded pack-file chunks that have no embedding. Retrieval accepts
only the fixture model tag and active documents for the case's pinned pack key and version.
Shared tenant knowledge and tool output are outside this slice.

This exercises the storage, scoping and citation paths without a provider or dependency.
Hash collisions and limited tokenization make it a lexical fixture. AI retrieval quality is
**built, not measured**. Any future embedding model needs a distinct tag and re-embedding.
The SQL encoder is deterministic code and makes no LLM call, so it creates no AI run.
Returned chunks are untrusted data and must be delimited before use in a prompt.
