# ADR 0008 — Record applied SQL migrations with checksums

**Status:** Accepted

**Date:** 2026-11-13

## Decision

The schema owner records the version, filename, SHA-256 of the source file, and database time in a private ledger. The runner takes a session advisory lock and validates the full recorded prefix before applying pending files. It inserts each ledger row inside the migration transaction. Migrations use one outer `BEGIN;` and `COMMIT;` and contain SQL only.

## Consequences

A rerun skips an identical applied prefix. A changed, renamed, missing, or out-of-order migration blocks the entire run before pending SQL executes. An existing engine schema without a ledger requires an explicitly reviewed baseline. Bootstrap and demo seeding remain separate operations.

The runner separates the source manifest from the ledger's database-generated timestamp. It sends each migration body as a single SQL block inside an explicit transaction, so embedded transaction commands cannot commit independently of the ledger write. The disposable harness checks both a body error and an attempted inline commit.
