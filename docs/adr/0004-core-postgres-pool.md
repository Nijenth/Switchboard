# ADR 0004 — Use a bounded PostgreSQL pool in the core service

**Status:** Accepted

**Date:** 2026-11-13

## Decision

Run the Stage 1 core on Python 3.12 with FastAPI and Pydantic v2, served by Uvicorn. Use Psycopg 3 and its pool for PostgreSQL access. HTTPX drives the FastAPI app in process in pytest. Ruff and mypy check the Python code.

Core opens a bounded pool as `switchboard_core`. Each ingress request resolves its credential and sets transaction-local tenant context inside the same transaction as the case, message, and audit writes. Health checks use the same login. Stage 1 runs PostgreSQL, so health checks only PostgreSQL until other dependencies are introduced.

## Consequences

The application owns its transaction boundaries and a pool can reuse a physical connection after commit or rollback. Integration tests can prove that reuse with a one-connection pool and real PostgreSQL. The database setup and cleanup stay separate from the running service.
