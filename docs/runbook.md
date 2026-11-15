# Switchboard runbook

## Local n8n visual editor

Run `docker compose -p switchboard-editor -f docker-compose.n8n-dev.yml up -d` and open
`http://127.0.0.1:5678`. Complete the local owner setup in the browser. The service imports
the inactive workflow exports on first startup and preserves your edits in a named volume.
It imports no credentials. Keep Gmail and schedules inactive while inspecting the workflows.

HTTP nodes use named Header Auth credentials with header name `Authorization` and a bearer
value scoped to the node's endpoint. The mock PostgreSQL nodes use the separate mock login.
Edit the core base URL in `wf_service_urls` for your running core and Slack fixture. The
checkpoint service uses its own URL. Publish the subworkflows when testing execution:
`wf_service_urls`, `wf_lookups_run`, `wf_tool_order_identity_check`, `wf_tool_order_lookup` and
`wf_tool_send_reply`. The lookup runner uses a "Switchboard Lookup" Header Auth credential scoped
to `/lookups`. Keep node environment access blocked. The disposable harness binds
generated credentials and loopback fixture URLs automatically.

To refresh the editor from Git, first preview or save any local edits. The import replaces
workflows with matching IDs and preserves the editor volume and credentials:

```sh
docker compose -p switchboard-editor -f docker-compose.n8n-dev.yml stop n8n-editor
docker compose -p switchboard-editor -f docker-compose.n8n-dev.yml run --rm --no-deps --entrypoint n8n n8n-editor import:workflow --separate --input=/switchboard/workflows
docker compose -p switchboard-editor -f docker-compose.n8n-dev.yml up -d
```

Use `python scripts/export_workflows` to preview changed file names and add `--write` to save
the sanitized exports. The script exports existing repository workflows by ID, strips editor
credential bindings and pinned data, restores repository placeholder bindings, and writes
inactive exports. Review the diff before committing and keep all secrets in n8n credentials.

The Gmail-shaped manual trigger enters the same case pipeline as the Gmail trigger. Core
owns drafting, policy, and queueing. Slack approval and mock dispatch run as separate
workflows so a reply can wait for a reviewer. The SLA watchdog calls the existing database
expiry transition through core. Refund action approvals can be posted by the same Slack
workflow. Mock dispatch covers replies and approved order refunds.
Run `bash scripts/test_core.sh --offline-n8n` with the pinned images already cached to
exercise the synthetic email through Slack and the mock receipt.

## Pack lookups and identity (ADRs 0019, 0021, 0022, 0023)

Set `SWITCHBOARD_LOOKUP_CREDENTIALS` to a JSON list of tenant IDs and tokens. After
`/understand` stores a validated extraction, call `POST /lookups` with the case and message IDs.
Run each `pending` action through `/actions/{id}/claim`, `/consume` and `/complete` with a worker
credential, sending the tool result as `output`. Call `/lookups` again until `pending` is empty.
`waiting` explains anything not proposed: `identity_unverified` means the identity check did not
verify the sender, and `input_unavailable` means no validated extraction supplied the inputs.

A 409 at claim or consume can indicate missing identity authority. Verify the sender for the
specific inbound message and order before attempting dispatch. For a 422 from lookup completion,
correct the connector result and submit it with the same consumed grant before the lease expires.
After expiry, claim the read action again. A failed or unknown lookup stays in `waiting` with
its status because lookup keys are fixed per message.
After migration 023, check the tool registry if a verified message still gets a 409. Every
identity-gated tool must require `order_number` in its input schema. Older refund actions stay
blocked from dispatch, including those with an outstanding grant. The refund slice will
publish replacement tool keys with the required contract.
If a lookup completes while `/drafts` waits for the model, `/drafts` returns 409
`Draft evidence changed`; call it again to draft from the new result.

## Mock order refunds

Apply migration 024 and run the demo seed and pack loader against a fresh demo database.
The seed pins Northwind to ecommerce support v3. Bind a separate tenant-scoped
`SWITCHBOARD_REFUND_CREDENTIALS` token to the Refund Header Auth placeholder. Import the
eligibility and refund-issue workflows with the mock Postgres credential and refresh the
case pipeline, lookup runner, dispatcher, and reconciler.

A proposal returning `current_eligibility_required` needs a completed eligibility check
for the same message and order. A proposal with an ineligible result or a policy block
creates no money action. `order_refund_already_exists` means a different message already
reserved the order. A replay of the original proposal returns its action and approval IDs.

The manager reviews the amount and currency in Slack within the tenant SLA. A 409 at
claim or consume can mean a newer eligibility result revoked authority. A lost completion
after payout leaves the action executing until the lease expires. Run the reconciler
to check the mock receipt and record the result. Keep ambiguous refunds in reconciliation
and use the existing 30-second grace period before declaring a missing receipt.
An expired refund approval cannot be approved. Running `POST /actions/approvals/expire`
(or `public.expire_approval_request`) marks the request `expired`, cancels the pending
refund action, releases the per-order live lock, and lets a fresh inbound message propose
a new refund. Reviewer edits that change a refund amount or currency are refused with
`409`, leaving the original request pending.

Apply migration 026 and deploy core before reimporting `wf_sla_watchdog`.
The workflow calls `POST /actions/approvals/expire-unclaimed` with the approval
poster credential to cancel approved actions that have never been claimed.
The window starts at approval and uses the tenant's
`approval_claim_window_minutes` (default 60). Look for an `action.cancelled`
event with reason `approval_claim_window_elapsed` when a fresh message can
propose a refund again. If the action already has a claim, use the reconciliation
procedure below and keep the reservation while evidence is inconclusive.

Deploy migration 027 and the matching core version together. Pause approvals
while applying the migration and replacing core because the older API expects
to promote the action itself. If promotion returns `409`, inspect the review role,
current policy and refund eligibility. The request stays pending and the normal
SLA applies. Migration 027 runs without a backfill. The migration 026 watchdog
cancels older approved requests whose actions remained unclaimed, including
actions still in `pending_approval`.

## Offline drafting

Apply migration 018 and set `SWITCHBOARD_DRAFT_CREDENTIALS` to a JSON list of tenant IDs and
dedicated 64-character hex bearer tokens. After `/understand`, call `POST /drafts` with the case
and latest inbound message IDs. The offline alias receives redacted customer text and retrieved
knowledge in separate untrusted-data blocks. Completed lookups whose pack entry declares an
`evidence` template appear first in that block, as `tool:<action id>` sentences (ADR 0020).

Core checks all cited IDs against the retrieved set. Automatic drafting requires an exact
sentence from the cited evidence, no detected PII, no order reference unless it appears in a
cited tool result, no detected
unsupported promise or injection, and a passing tone check. These offline heuristics need
evaluation before use with customer data. Invalid citations or failed checks leave the draft
pending. If the model returns no usable result, the endpoint returns a review reason without
storing a draft. Repeat requests return the existing live draft. The database also enforces
one live draft per inbound message.

An automatic send policy requires passing G6 and G7 on the same hashed draft payload. The
endpoint records the draft, policy decision, and audit event in one transaction. It does not
dispatch a reply. Existing automatic decisions without passing evidence require a new policy
decision before dispatch. The offline drafting quality status is **built, not measured**.

## Offline knowledge search

Apply migration 017 before publishing pack knowledge with the loader. Set
`SWITCHBOARD_RETRIEVE_CREDENTIALS` to a JSON list of tenant IDs and dedicated 64-character
hex bearer tokens. Call `POST /retrieve` with the inbound `case_id` and `message_id` and an
optional `limit` from 1 to 5. The query comes from the stored redacted body. Results contain
chunk IDs for citations, document titles, untrusted content, and a fixture similarity score.
Empty queries or no positive matches return an empty list. A case without an enabled pinned
pack is unavailable for retrieval.

Verify ranking and scope in the disposable PostgreSQL suite. The token-hash encoder is
offline and its ranking is **built, not measured**. See ADR 0014.

Later work includes paragraph-level knowledge chunks and expected-document checks in evals.

## Phase 2 offline AI checks

Build the disposable database image and pull the pinned gateway images with
`docker compose --profile offline-ai pull litellm fake-model`. With the Python 3.12
workspace environment active, run `scripts/test_core.sh --offline-ai`. The harness
starts a fresh disposable cluster, exercises the gateway over loopback, and compares both
provider aliases on two repeats of the published ecommerce fixtures. It removes the test
services on exit. The reported costs and latencies are fixture data and local timings.
The fake server replays fixture labels. Its accuracy and agreement do not measure a model.
The gate evaluates the same policy engine as `/decide`. Until drafting and retrieval are wired
in, outbound proposals enter review because verified citations and output checks are unavailable.
The report counts intent accuracy only for classified attempts and lists guardrail escalations
separately.

### Offline ecommerce decisions

Set `SWITCHBOARD_DECIDE_CREDENTIALS` to a JSON list of tenant IDs and separate 64-character
hex bearer tokens. Call `POST /decide` with the case ID and latest inbound message ID after
`/understand`. An optional `proposed_tools` list names tools to evaluate. Core gets identity,
guardrail flags, confidence, tool permissions, and budget state from the database. It records
the evaluated inputs and policy revision. Replaying the current decision with the same inputs
returns the existing revision.

The endpoint writes decisions and audit entries. Drafting, tool dispatch, and the workflow
failure-mode gate follow the decision-pipeline review.

`/understand` defaults to the primary named by `SWITCHBOARD_UNDERSTAND_PRIMARY`. Retry a
completed message using the original request body to replay the result. To explicitly run it
again, provide a fresh `rerun_key` UUID and reuse that key on retries. HTTP 409 means a claim
has no completed result. Check its `ai_runs` entries before authorizing a rerun of an interrupted
claim. An ambiguous model charge remains reserved for budget admission.

Keep the provider keys empty for this run. Future live configuration reads `OPENAI_API_KEY`
and `ANTHROPIC_API_KEY` from local `.env` after model and spending approval. Langfuse stays
disabled. See ADR 0011.

### Before the first live synthetic eval

- Confirm the selected model IDs, provider keys, and provider-side spending caps. Verify input
  and output prices, token bounds after LiteLLM translation, output limits, and conservative
  reservation accounting for timeouts, missing costs, and fallback. Offline fixture prices
  apply only to the fake gateway.
- Inspect a sanitized capture of the translated Haiku 5.5 request. Confirm native structured
  output, low effort, and no temperature parameter. Exercise schema rejection and fallback.
- Inspect the translated GPT-6 Luna request. Confirm reasoning is disabled, temperature
  is at most 0.2, and the selected endpoint accepts the schema and output-token limit.
- Use synthetic cases only. Run each twice, report agreement, and keep Langfuse disabled.
  Real customer data requires the stronger redactor and its independent recall check.

The local rules cover name cues, street formats and postcodes in addition to
known sender names and structured PII. Run `python -m pytest -qs tests/test_pii_probes.py`
from `services/core` when changing the rules. The offline pack eval also fails
on any of its inherited synthetic PII misses. Diagnose misses using case IDs
and kinds instead of printing the spans. See [ADR 0028](adr/0028-rules-based-pii-redaction.md)
for the measured results and the independent evaluation needed before real data.

Prototype Microsoft Presidio with locally installed language-specific NER models and custom
address candidate recognizers. Use libpostal to parse the candidate spans, including
house-number-last forms. Both can run locally without API fees. Compare span recall and
false positives against independent multilingual fixtures before enabling real data.
[Presidio Analyzer](https://microsoft.github.io/presidio/analyzer/).
[libpostal](https://github.com/openvenues/libpostal).

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
