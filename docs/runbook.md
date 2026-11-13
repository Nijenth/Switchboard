# Switchboard runbook

## Run the database safety suite

Copy `.env.example` to `.env` and fill in the six local test passwords. Use single-line, distinct random values. Keep `.env` local.

From the repository root, with Docker Desktop running in Linux container mode, run:

```powershell
docker compose --env-file .env --profile test run --rm --no-deps db-check
```

The command creates a new PostgreSQL 16 cluster on temporary storage with no network and no published ports. The bootstrap superuser creates the database, roles, and extensions. The non-superuser schema owner applies the migrations and demo seed, then runs `db/tests/safety_checks.sql`. The core, mock, and reporting checks each open a separate connection with the respective login. Every connection uses the same private socket. Compose removes the container when the command exits.

On a nonzero exit, use the first reported failure to identify the step that stopped. A missing password fails before PostgreSQL starts. A SQL error stops migration, seed, or role setup immediately. The safety suite prints each check and raises an error if any check fails. Each invocation starts with a fresh cluster, so a rerun after correcting the cause needs no reset.

## Run the Stage 1 core

From `services/core`, use Python 3.12 to install `requirements.txt` in a virtual environment. Set `SWITCHBOARD_DB_HOST`, `SWITCHBOARD_DB_PORT`, `SWITCHBOARD_DB_NAME`, `SWITCHBOARD_CORE_PASSWORD`, and `SWITCHBOARD_HEALTH_TOKEN` in the process environment. Core always connects as `switchboard_core`. Use a randomly generated health token of at least 32 ASCII characters. Run `python -m uvicorn app.main:create_app --factory --host 127.0.0.1` for local development.

For each private `/ingest` caller, generate a separate 32-byte random token encoded as 64 lowercase hex characters. Store the token with the caller. As the configuration loader, register the SHA-256 digest of the encoded token, its tenant and endpoint IDs, and an expiry in `ingress_credentials`. Use a parameterized write for the digest. Set `revoked_at` to revoke a credential.

Send the token in `Authorization: Bearer …` with the canonical JSON body. The credential determines the tenant and endpoint. A successful retry returns the original case, message, and trace IDs with `duplicate: true`. The Stage 1 API accepts text and form fields. It rejects nonempty attachments until document storage is implemented. The case-level pack pin remains stable across pack upgrades. Disabling the enrollment stops ingress and new routing to it.

An idempotency key is scoped to the authenticated endpoint. A retry must preserve the normalized delivery, including its received time. Trace IDs and the optional matching tenant claim may differ. Reusing the key for a different delivery returns HTTP 409. Existing rows without a digest return 409 on key reuse because their original delivery cannot be verified.

Migration tracking with checksums is required before applying the migrations to a persistent database.

Deferred engineering work includes structured JSON logs with `trace_id`, a Bash integration runner for CI, and a dependency lockfile.

### Run the core integration tests

Create a repository-root `.venv` with Python 3.12 and install `services/core/requirements-dev.txt` into it. With Docker Desktop running and the pgvector image from the SQL suite available locally, run `./scripts/test_core.ps1` from PowerShell.

The script provisions a fresh PostgreSQL cluster with generated credentials. It publishes a temporary port on `127.0.0.1` and checks the cluster's randomly generated marker before tests write fixtures or send requests. The test app runs in process. The script removes its container and anonymous volumes on exit. A startup error stops the run before pytest, and a pytest failure makes the script exit nonzero.
