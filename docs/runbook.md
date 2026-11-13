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

### Apply migrations to an initialized database

Run `bash scripts/migrate` as the schema owner with `SWITCHBOARD_DB_SOCKET`, `SWITCHBOARD_DB_OWNER_PASSWORD`, and `SWITCHBOARD_MIGRATION_DATABASE` pointing at the intended database. Provision roles and extensions first. Keep the demo seed command separate. The runner checks the entire applied history under a database lock, then writes each migration and its checksum in the same transaction. Use one outer `BEGIN;` and `COMMIT;` per migration, with transactional SQL and no psql commands inside.

If the ledger differs from the repository, recover the exact applied file and put the change in a new migration. A database with an existing Switchboard schema and no ledger needs an independently verified baseline before further migrations.

Deferred engineering work includes structured JSON logs with `trace_id`, a Bash integration runner for CI, and a dependency lockfile.

### Run the core integration tests

Create a repository-root `.venv` with Python 3.12 and install `services/core/requirements-dev.txt` into it. With Docker Desktop running and the pgvector image from the SQL suite available locally, run `./scripts/test_core.ps1` from PowerShell.

The script provisions a fresh PostgreSQL cluster with generated credentials. It publishes a temporary port on `127.0.0.1` and checks the cluster's randomly generated marker before tests write fixtures or send requests. The test app runs in process. The script removes its container and anonymous volumes on exit. A startup error stops the run before pytest, and a pytest failure makes the script exit nonzero.

## Stage 2 approval and execution checks

Core must verify a reviewer interaction and resolve the reviewer before calling the database decision function. The database checks the reviewer's active role, tenant, request deadline, and exact subject hash. The SLA watchdog calls the expiry function in a tenant-scoped transaction. An edit or escalation retires the original action or draft and creates a fresh request. Action edits are limited to amount and currency. Draft edits are limited to subject and body. Escalation requires an admin on the successor request.

Automatic claims require the current case decision to allow the tool. Registered reads are exempt from this prior decision requirement and still require an enabled pack and verified identity when the registry requires it. External communications require a same-case authorized draft. Failed actions cannot be reclaimed. Leave ambiguous attempts for reconciliation and keep automated retries disabled until the reconciliation path records the evidence needed to authorize a retry.

If migration `004_execution_authorization.sql` reports existing execution records, stop. It requires an explicit migration plan to preserve old approvals and payload bindings. The current test runner uses a fresh cluster with no execution records.

### Slack reviewer callback

Inject `SWITCHBOARD_SLACK_APP_ID` and `SWITCHBOARD_SLACK_SIGNING_SECRET` into core. Configure the tenant's `slack_workspace_id` and its staff `slack_user_id` values through the configuration role. Persist the original channel and message timestamp on each approval request before accepting buttons. Use `POST /approvals/slack` with the original signed form body. The button IDs are `switchboard_approve`, `switchboard_reject`, and `switchboard_escalate`. Their value is the request UUID.

### Mock execution and reconciliation

Inject `SWITCHBOARD_EXECUTION_CREDENTIALS` as a JSON array of entries with `tenant_id`, `role`, `principal`, and a distinct 64-character lowercase hex `token`. Use separate principals for workers and reconcilers. Keep the API on a protected transport and keep grants and response bodies out of execution logs.

A worker claims through `POST /actions/{id}/claim`, then sends the returned grant to `/consume` to obtain the approved payload. After the mock call, send the grant and `external_ref` to `/complete`. The lease lasts 45 seconds with no renewal. An expired, lost, or ambiguous attempt goes through `/reconcile` using the reconciler credential. Supply `outcome` and `lookup_ref` for each lookup result, and include `external_ref` for a found result. The adapter's lookup must establish the result before the reconciler submits it. A confirmed absence ends the attempt without permitting a new claim.

### Verify and checkpoint the audit chain

Set a distinct `SWITCHBOARD_AUDIT_TOKEN` on core and the checkpoint process. Set `SWITCHBOARD_AUDIT_URL` to the core `/audit/verify` URL. Give the checkpoint process a private directory outside the database and core service's writable storage. On a fresh verified deployment, initialize once with `python scripts/checkpoint_audit.py --initialize <history-path>`. Subsequent runs omit `--initialize`. On an HTTP 409 or missing history, preserve the existing history and investigate. An operator should clear a stale lock only after confirming that no checkpoint process is active. Configure the n8n schedule and protected storage before relying on the checkpoint in a deployment.
