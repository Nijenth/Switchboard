# ADR 0002 — Run database checks in a disposable PostgreSQL container

**Status:** Accepted
**Date:** 2026-11-13

## Context

Stage 0 needs PostgreSQL 16 with pgvector to run the existing migrations and safety suite. The suite needs an owner connection and changes data while it runs. A developer database may already contain data, and a rolled-back test can still advance a sequence.

## Decision

Use the upstream pgvector image in a test-only Compose profile. Each invocation initializes a fresh cluster on temporary storage, connects over its private Unix socket, and reads the SQL through read-only mounts. The container has no network interface for reaching another database. The bootstrap superuser creates the database, extensions, and roles. The non-superuser owner applies the migrations. Privilege checks authenticate using the application logins.

## Consequences

The runner can exercise database constraints, grants, and row-level security against real PostgreSQL. Its results apply to the disposable cluster and the fixture revision it loaded. Development and deployment services can be added as their stages need them.
