# Switchboard runbook

## Phase 2 offline AI checks

Build the disposable database image and pull the pinned gateway images with
`docker compose --profile offline-ai pull litellm fake-model`. With the Python 3.12
workspace environment active, run `scripts/test_core.sh --offline-ai`. The harness
starts a fresh disposable cluster, exercises the gateway over loopback, and compares both
provider aliases on two repeats of the published ecommerce fixtures. It removes the test
services on exit. The reported costs and latencies are fixture data and local timings.
The fake server replays fixture labels. Its accuracy and agreement do not measure a model.
The gate evaluates the understanding result, which remains approval-only or escalation until
the decision pipeline is implemented. Rerun the safety gate against that pipeline before
enabling automatic decisions.

Keep the provider keys empty for this run. Future live configuration reads `OPENAI_API_KEY`
and `ANTHROPIC_API_KEY` from local `.env` after model and spending approval. Langfuse stays
disabled. See ADR 0011.

Before real customer data, add a local multilingual PII detector with person-name recognition
and locale-specific address parsing. Combine it with the existing pattern and known-contact
checks, and evaluate recall on independently labeled multilingual messages. Hold content for
review when the detector is unavailable or uncertain. The current fixtures track name and
address misses by case ID and category without printing the spans.

## Run the database safety suite

Copy `.env.example` to `.env` and fill in the six local test passwords. Use single-line, distinct random values. Keep `.env` local.

From the repository root, with Docker Desktop running in Linux container mode, run:

```powershell
docker compose --env-file .env.example --profile test build db-check
docker compose --env-file .env --profile test run --pull never --rm --no-deps db-check
```

The command creates a new PostgreSQL 16 cluster on temporary storage with no network and no published ports. The bootstrap superuser creates the database, roles, and extensions. The schema owner applies migrations and seeds tenant and mock data. The config-loader login publishes the pack catalog. The suite checks safety invariants, the migration ledger, same-version pack edits, and the permissions of separately authenticated config-loader, core, mock, and reporting logins. Every connection uses the same private socket. Compose removes the container when the command exits. From Bash, source `scripts/test_db_env.sh` and use `.env.example` for the test command to generate disposable passwords.

On a nonzero exit, use the first reported failure to identify the step that stopped. A missing password fails before PostgreSQL starts. A SQL error stops migration, seed, or role setup immediately. The safety suite prints each check and raises an error if any check fails. Each invocation starts with a fresh cluster, so a rerun after correcting the cause needs no reset.

## Run the Stage 1 core

From `services/core`, use Python 3.12 to install `requirements.txt` in a virtual environment. Set `SWITCHBOARD_DB_HOST`, `SWITCHBOARD_DB_PORT`, `SWITCHBOARD_DB_NAME`, `SWITCHBOARD_CORE_PASSWORD`, and `SWITCHBOARD_HEALTH_TOKEN` in the process environment. Core always connects as `switchboard_core`. Use a randomly generated health token of at least 32 ASCII characters. Run `python -m uvicorn app.main:create_app --factory --host 127.0.0.1` for local development.

For each private `/ingest` caller, generate a separate 32-byte random token encoded as 64 lowercase hex characters. Store the token with the caller. As the configuration loader, register the SHA-256 digest of the encoded token, its tenant and endpoint IDs, and an expiry in `ingress_credentials`. Use a parameterized write for the digest. Set `revoked_at` to revoke a credential.

Send the token in `Authorization: Bearer …` with the canonical JSON body. The credential determines the tenant and endpoint. A successful retry returns the original case, message, and trace IDs with `duplicate: true`. The Stage 1 API accepts text and form fields. It rejects nonempty attachments until document storage is implemented. The case-level pack pin remains stable across pack upgrades. Disabling the enrollment stops ingress and new routing to it.

An idempotency key is scoped to the authenticated endpoint. A retry must preserve the normalized delivery, including its received time. Trace IDs and the optional matching tenant claim may differ. Reusing the key for a different delivery returns HTTP 409. Existing rows without a digest return 409 on key reuse because their original delivery cannot be verified.

### Apply migrations to an initialized database

Run `bash scripts/migrate` as the schema owner with `SWITCHBOARD_DB_SOCKET`, `SWITCHBOARD_DB_OWNER_PASSWORD`, and `SWITCHBOARD_MIGRATION_DATABASE` pointing at the intended database. Provision roles and extensions first. Keep the demo seed command separate. The runner checks the entire applied history under a database lock, then writes each migration and its checksum in the same transaction. Use one outer `BEGIN;` and `COMMIT;` per migration, with transactional SQL and no psql commands inside.

If the ledger differs from the repository, recover the exact applied file and put the change in a new migration. A database with an existing Switchboard schema and no ledger needs an independently verified baseline before further migrations.

Deferred engineering work includes structured JSON logs with `trace_id` and a dependency lockfile.

### Validate and publish packs

With Python 3.12 and `scripts/requirements-packs.txt` installed, run `python scripts/load_packs --check` from the repository root. This requires no database. To publish, omit `--check` and set the intended database host or socket, database name, and `SWITCHBOARD_CONFIG_LOADER_PASSWORD`. The loader authenticates as `switchboard_config_loader`. Sync pack knowledge only with an explicit `--knowledge-tenant` whose enrollment is enabled and pinned to that version.

If a released checksum differs, restore the released files or increment the version. A database containing packs from the old SQL seed needs a baseline review before the first file-based sync. See ADR 0010.

### Run the core integration tests

Create a repository-root `.venv` with Python 3.12 and install `services/core/requirements-dev.txt` into it. Build the SQL check image first. Run `./scripts/test_core.ps1` from PowerShell or activate the virtual environment and run `scripts/test_core.sh` from Bash.

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

To clear a stale `.lock`, first stop or pause the checkpoint schedule and confirm the checkpoint service has no active run. Keep the JSONL history intact. Remove only the sibling `<history-path>.lock`, run one normal checkpoint verification, and resume the schedule after it succeeds.

## Import the mock outbound workflows

Use n8n 2.46.7. Import the credential-free files in `workflows/` into the n8n instance, which must use its own database. Assign the `switchboard_n8n_mock` PostgreSQL credential to the two receipt nodes. That credential accesses `demo` only. Publish `wf_error_handler` and `wf_tool_send_reply` before publishing the schedule workflows. For CLI-only publication set `N8N_USE_WORKFLOW_PUBLICATION_SERVICE=false` consistently and restart n8n after publishing.

In a dedicated n8n instance with trusted workflow editors, inject `SWITCHBOARD_CORE_URL`, `SWITCHBOARD_APPROVAL_POSTER_TOKEN`, `SWITCHBOARD_WORKER_TOKEN`, `SWITCHBOARD_RECONCILER_TOKEN`, `SWITCHBOARD_SLACK_POST_URL`, and `SWITCHBOARD_SLACK_BOT_TOKEN`. Allow environment access for node expressions in that instance. Keep success, error, manual, and progress execution data disabled because it can contain grants and approved draft content. Use one workflow deployment per tenant credential set. The case queue must have a configured Slack channel. For a live Slack workspace, set the Slack POST URL to its `chat.postMessage` endpoint and configure the app to send signed button callbacks to core.

Any new case policy revision invalidates an outstanding grant for dispatch. On the next ten-second tick, n8n re-claims unconsumed work for the same worker. Core revalidates authority within the original lease. If core refuses re-claim, the workflow leaves the attempt for reconciliation. The reconciler can report a found receipt after expiry. Core accepts absent evidence only after an additional 30 seconds.

Run `scripts/serve_audit_checkpoint.py` under the separate checkpoint OS identity, with `SWITCHBOARD_CHECKPOINT_HISTORY`, `SWITCHBOARD_AUDIT_URL`, `SWITCHBOARD_AUDIT_TOKEN`, and a distinct `SWITCHBOARD_CHECKPOINT_SCHEDULE_TOKEN`. The default listener is loopback. Use a private protected route to its `/checkpoint` endpoint for n8n, with `SWITCHBOARD_CHECKPOINT_URL` and the schedule token. Only the checkpoint identity receives write access to the history. n8n receives neither that access nor the audit token.

Run `./scripts/test_core.ps1 -WithN8n` for the disposable workflow test. It uses the pinned Docker image `docker.n8n.io/n8nio/n8n:2.46.7`, imports the exports into a temporary n8n database, uses a local Slack fixture and a real PostgreSQL mock login, and cleans up its test volume. The test uses manual triggers and a separate checkpoint process. Verify live schedules, queue-mode workers, and checkpoint account permissions when deploying.
