# AGENTS.md — Multi-Purpose AI Operations Platform

Source of truth for any coding agent (Codex, Claude, etc.) working in this repo.
Read it fully before making changes. If a request conflicts with a rule here, stop and ask.

---

## 1. What we are building

A **multi-tenant AI operations engine** that takes incoming work (emails, chats, web forms,
documents, API calls), understands it, takes action in business tools under strict rules,
asks a human when needed, and records every decision.

The engine is **generic**. Each business use case is a **pack**: a folder of configuration
(categories, extraction schemas, allowed tools, business rules, prompts, knowledge, eval cases).
Adding a use case means adding a pack, not changing the engine.

**Launch packs** (all used by the demo tenant "Northwind Apparel", a fictional clothing brand):

| Pack key | Use case | Input | Typical outcome |
|---|---|---|---|
| `ecommerce_support` | Customer support | Email, web chat | Order status reply, refund proposal, escalation |
| `sales_leads` | Lead qualification | Web form, email | Lead scored, CRM updated, meeting booked, sales alerted |
| `invoice_processing` | Accounts payable | Email with PDF | Fields extracted, matched to PO, posted or sent for finance approval |

**Golden rule:** design the engine generically, but **finish one pack completely before starting the next.**

**Non-goals:** a general chatbot; AI that moves money or messages customers without passing the policy engine.

---

## 2. Architecture

```
Inputs: email · web chat · web form · API · (WhatsApp, Slack later)
  │  signed webhooks, rate limits
  ▼
INGEST     normalize → dedupe (idempotency key) → store files in MinIO → create/attach CASE
ROUTE      channel endpoint → pack   (or AI router when the endpoint is shared)
GUARD IN   PII redaction · prompt-injection check · language detection
UNDERSTAND classify (pack intents) + extract (pack schema, per-field confidence)
RETRIEVE   RAG over tenant + pack knowledge · read-only tools (order lookup, PO lookup…)
DECIDE     policy engine: global safety rules → pack rules → tenant overrides
ACT        tool calls via registry (risk-checked) · drafts for outbound messages
GUARD OUT  grounded? within policy? no PII leak? tone?
APPROVE    Slack approval request with SLA → approve / edit / reject / escalate
RECORD     audit_log · ai_runs · metrics views · Langfuse trace
```

- n8n keeps its own internal state in a **separate database** (`n8n`) on the same Postgres server.
- n8n runs in **queue mode**: 1 main + ≥1 worker + Redis.
- **Postgres + pgvector** is the system of record. Schema `public` = engine. Schema `demo` = mock
  external systems (shop, CRM, accounting, calendar) used until real integrations are connected.

### 2.1 Responsibility boundary: n8n vs core service (decided — ADR 0001)

**This is an n8n-based platform, not a replacement for n8n.** We do not rebuild triggers, scheduling,
retries or connectors. The core service exists only for what n8n cannot be trusted with:
deciding, authorizing and recording.

| n8n owns (the hands) | Core service owns (the brain + the ledger) |
|---|---|
| Triggers: email, webhooks, forms, chat, schedules | Pack loading and validation |
| Step sequencing, step retries, execution history | All AI calls (via LiteLLM), guardrails, PII redaction |
| Calling external APIs via its connectors (Shopify, HubSpot, QuickBooks, Gmail, Slack, Cal.com) | Policy engine: every decision and every permission check |
| Slack approval buttons and message updates | Action authorization: claim, execution grant, completion |
| SLA timers, reconciler and audit-verify schedules | **Sole writer** of all engine tables (`public` schema) |
| Mock tool sub-workflows (read/write `demo` schema only) | Audit log, metrics, eval runner |

Hard rules:
- **n8n never connects to engine tables.** It calls the core API. Its DB role (`app_n8n_mock`) can only see `demo`.
- **n8n never decides.** No IF-node business logic about whether something is allowed. It asks `/decide` and `/actions/{id}/claim`.
- **Workflows are pack-agnostic.** Pack differences live in pack files and the core service.

### 2.2 Tool execution protocol (crash-safe, no double execution)

```
1. core  /decide              → creates action rows (status proposed | pending_approval)
2. human approves in Slack    → n8n posts to core /approvals/{id}/decide (role + payload checked in DB)
3. n8n   /actions/{id}/claim  → core atomically sets status=executing with worker id + lease
                                (only one claimer wins), returns a single-use execution grant
4. n8n   wf_tool_<key>        → calls the external API, passing the action's idempotency key
                                where the API supports it
5. n8n   /actions/{id}/complete → core records executed | failed (+ external_ref)
6. crash between 4 and 5      → lease expires → wf_action_reconciler marks unknown_outcome,
                                checks the external system, then records executed or failed.
                                unknown_outcome can NEVER be retried blindly (enforced in DB).
```

---

## 3. Tech stack

| Concern | Choice |
|---|---|
| Orchestration | n8n (self-hosted, Docker, queue mode) |
| Core service | Python 3.12, FastAPI, Pydantic v2, pytest, ruff, mypy |
| Database | PostgreSQL 16+ with pgvector, pgcrypto, citext |
| Queue | Redis 7 |
| File storage | MinIO (S3-compatible; swap for AWS S3 in production via env vars) |
| AI gateway | LiteLLM: one API for all providers, fallback models, budgets, cost tracking |
| Documents | PyMuPDF for text PDFs; vision-capable LLM for scanned docs/images |
| Embeddings | 1536-dim model. Changing dimension = new migration + re-embed |
| AI tracing | Langfuse (Cloud free tier in dev; self-hosted on the demo server) |
| Dashboards | Metabase on `v_*` views |
| Approvals | Slack interactive messages |
| Integrations | Shopify dev store, Gmail, HubSpot (free CRM), Cal.com, QuickBooks Online sandbox or Xero demo |
| CI | GitHub Actions: ruff, mypy, pytest, pack validation, eval gates |
| Deploy (demo) | Single VPS + Docker Compose + Caddy (auto HTTPS) |

---

## 4. Repository layout

```
AGENTS.md
docker-compose.yml            # n8n main+worker, redis, postgres, minio, litellm, metabase
docker-compose.demo.yml       # adds caddy + self-hosted langfuse for the VPS
.env.example                  # every env var documented; real .env is git-ignored
db/
  migrations/                 # 001_init.sql, 002_security_hardening.sql … forward-only; never edit an applied file
  seeds/                      # demo data only
  tests/safety_checks.sql     # executable DB invariant tests; must pass in CI (exit code ≠ 0 on failure)
packs/
  ecommerce_support/
  sales_leads/
  invoice_processing/
services/core/
  app/
    api/                      # FastAPI routes (see §8)
    engine/                   # pack loader, rule engine, decision pipeline (pure logic, no I/O)
    guardrails/
    tools/                    # tool registry, input/output schemas, authorization + contract tests
                              # (execution lives in n8n: workflows/wf_tool_<key>.json, mock + real branches)
    extraction/               # document + message extraction
    adapters/                 # LiteLLM client, DB, MinIO, Slack, Shopify, HubSpot, QuickBooks…
    schemas/                  # Pydantic models
  tests/
workflows/                    # exported n8n JSON (credentials stripped)
evals/runner/                 # runs every pack's eval cases, writes eval_runs/eval_results
evals/fixtures/               # sample PDFs/images for document eval cases
docs/                         # architecture.md, runbook.md, adr/0001-*.md, packs.md
scripts/                      # migrate, seed, load_packs, export_workflows, run_evals
```

---

## 5. Packs

### 5.1 Folder format

```
packs/<pack_key>/
  pack.yaml            # identity, channels, intents, thresholds, default queue
  schemas/*.json       # JSON Schemas for extraction and outputs (versioned: invoice.v1.json)
  tools.yaml           # tools this pack may use + approval requirements
  rules.yaml           # pack business rules (declarative; see 5.4)
  prompts/*.md         # versioned prompts: classify.v1.md, extract.v1.md, draft_reply.v1.md
  knowledge/*.md       # docs to embed for RAG (policies, FAQs, price lists)
  evals/cases.jsonl    # golden test cases
  README.md            # what the pack does, demo script, metrics it reports
```

`scripts/load_packs` validates every pack (schema-checks YAML, checks that referenced tools and files
exist, checks that rules only reference known fields) and syncs it into `packs`, `pack_intents`,
`pack_tools`, knowledge tables and `eval_cases`. **CI fails if a pack does not validate.**
Packs are versioned: change anything → bump `version` → new `packs` row. Tenants pin a version.

### 5.2 `pack.yaml` example

```yaml
key: invoice_processing
version: 1
name: Invoice Processing
description: Extract supplier invoices, match to purchase orders, route for payment approval.
input_channels: [email, api]
accepts_documents: [application/pdf, image/png, image/jpeg]
default_queue: accounts_payable
intents:
  - { key: supplier_invoice, description: "A bill requesting payment", default_priority: normal }
  - { key: credit_note,      description: "A credit against a prior invoice", default_priority: normal }
  - { key: payment_reminder, description: "Supplier chasing an unpaid invoice", default_priority: high }
  - { key: not_an_invoice,   description: "Anything else sent to this inbox", default_priority: low }
extraction:
  schema: schemas/invoice.v1.json
  required_fields: [vendor_name, invoice_number, invoice_date, total_amount, currency]
prompts:
  classify: prompts/classify.v1.md
  extract:  prompts/extract.v1.md
thresholds:
  auto_confidence: 0.85     # min classification confidence for auto decisions
  field_confidence: 0.90    # min confidence for each required extracted field
```

### 5.3 `tools.yaml` example

```yaml
tools:
  - key: vendor_lookup
  - key: po_lookup
  - key: invoice_duplicate_check
  - key: invoice_post_to_accounting        # write
  - key: invoice_schedule_payment          # money → always approval
    approval: { role: finance }
  - key: notify_slack
```

### 5.4 `rules.yaml` and the condition language

Rules are **data, not code**. The engine evaluates them with a small safe condition language.
Never use Python `eval`/`exec` on rule content.

Condition operators: `all`, `any`, `not`, and leaf `{ field, op, value }` where
`op ∈ eq, ne, gt, gte, lt, lte, in, not_in, contains, exists`.
Fields are dotted paths into the decision context:
`classification.*`, `extraction.*`, `checks.*` (tool results), `case.*`, `contact.*`, `tenant.*`.

```yaml
version: 1
rules:
  - id: INV_DUPLICATE
    when: { field: checks.duplicate_invoice, op: eq, value: true }
    then: { decision: escalate, queue: accounts_payable, reason: "Possible duplicate invoice" }

  - id: INV_NO_PO
    when: { field: checks.po_found, op: eq, value: false }
    then: { decision: needs_approval, role: finance, reason: "No matching purchase order" }

  - id: INV_PO_VARIANCE
    when: { field: checks.po_variance_pct, op: gt, value: 2 }
    then: { decision: needs_approval, role: finance, reason: "Total differs from PO by more than 2%" }

  - id: INV_HIGH_VALUE
    when: { field: extraction.total_amount, op: gte, value: 5000 }
    then: { decision: needs_approval, role: manager, reason: "High-value invoice" }

  - id: INV_CLEAN_MATCH
    when:
      all:
        - { field: checks.po_found, op: eq, value: true }
        - { field: checks.po_variance_pct, op: lte, value: 2 }
    then: { decision: auto, allow_tools: [invoice_post_to_accounting], reason: "Clean PO match" }
```

---

## 6. Decision pipeline (policy engine)

Decisions: `auto`, `needs_approval`, `escalate`, `ignore` (spam, auto-replies, out-of-office).

Evaluated in this order. The first rule that matches decides, **except** that later layers may only make
a decision *stricter*, never looser. Strictness: `escalate > needs_approval > auto > ignore`.

1. **Global safety rules** (engine code, cannot be overridden by packs or tenants):
   - G1 Prompt injection, legal threat, chargeback, safety or harassment flag → `escalate` (urgent)
   - G2 A tool with `requires_verified_identity` is needed and identity is not verified → ask for
     verification only. No data from that tool may appear in any output.
   - G3 Any `money` tool → `needs_approval` (role from `pack_tools`, default `manager`)
   - G4 Classification confidence < pack `auto_confidence` → `needs_approval`
   - G5 Any required extraction field below `field_confidence`, or failing schema validation → `needs_approval`
   - G6 Outbound text with no citation (knowledge chunk or tool result) → `needs_approval`
   - G7 Output guardrail failure (PII leak, promise beyond policy, tone) → `needs_approval`
   - G8 Tenant monthly AI budget exceeded → `needs_approval` with no further LLM calls
2. **Pack rules** (`rules.yaml`), in file order.
3. **Tenant overrides** (`tenant_packs.config.rules`). These can only tighten.
4. **Default:** `needs_approval`.

Every decision writes `policy_decisions` with `rule_id`, `rule_source` (`global|pack|tenant`),
`policy_version` and an `inputs` snapshot. **Every rule has unit tests**, both matching and non-matching.

---

## 7. Tools

Every action the engine can take is a registered tool (`tools` table, implemented in `services/core/app/tools/<key>/`).

| Risk level | Meaning | Approval |
|---|---|---|
| `read` | Looks data up, changes nothing | Never |
| `write` | Creates or updates records (CRM contact, accounting entry, ticket) | Only if the decision is `auto` and a pack rule allows the tool |
| `external_comm` | Sends something outside the company (email reply, booking confirmation) | Must have an approved or `auto` draft |
| `money` | Refunds, payments, discounts | **Always**, by the required role. The DB blocks execution without an approver |

Each tool has:
- input/output JSON Schemas, a timeout, an idempotency key and retry policy
- an n8n sub-workflow `wf_tool_<key>` with **two branches:** `mock` (reads/writes the `demo` schema)
  and `real` (Shopify, HubSpot, QuickBooks…). The branch comes from the execution grant, which core
  builds from `tenant_packs.config.adapters`. Both branches must pass the same contract tests.
- **Mock can never reach a real system:** the mock branch has no real credentials attached, and the
  real branch refuses grants marked `mock`.

The LLM may **propose** tool calls. Only the engine **authorizes** them (§2.2), and the database
enforces the result (§10, "Database-enforced invariants").

---

## 8. Core service API (FastAPI)

```
POST /ingest                 canonical input → case + message (idempotent)
POST /route                  pick pack for a case (endpoint mapping or AI router)
POST /guardrails/in          redact PII, detect injection, detect language
POST /understand             classify + extract with the case's pack
POST /retrieve               RAG search scoped to tenant + pack
POST /decide                 run the decision pipeline → policy_decision (+ proposed actions)
POST /actions/{id}/claim     atomically claim an approved/auto-allowed action → single-use execution grant
POST /actions/{id}/complete  record result of an execution grant (executed | failed + external_ref)
POST /actions/reconcile      lease-expired 'executing' → unknown_outcome → verify externally → final state
POST /drafts                 generate an outbound draft with citations + output guardrails
POST /approvals/{id}/decide  record a reviewer decision (role-checked)
POST /documents              upload a file → MinIO + documents row
GET  /health                 dependency checks (db, redis, minio, litellm)
```

All endpoints require a service token, take and return `trace_id`, and validate with Pydantic.

---

## 9. Canonical input schema

```json
{
  "tenant_id": "uuid | null      // resolved from channel endpoint if null",
  "trace_id": "uuid",
  "idempotency_key": "string     // channel + external id, or sha256(channel, sender, body, minute)",
  "channel": "email | web_chat | web_form | api | whatsapp | slack",
  "endpoint_address": "string    // e.g. invoices@northwind.example or form id 'wholesale-enquiry'",
  "external_message_id": "string | null",
  "external_thread_id": "string | null",
  "sender": { "address": "string", "name": "string | null" },
  "subject": "string | null",
  "body_text": "string",
  "form_fields": {},
  "attachments": [{ "filename": "string", "mime_type": "string", "size_bytes": 0, "storage_key": "string" }],
  "received_at": "ISO-8601"
}
```

---

## 10. Non-negotiable engineering rules

### Reliability
- Every external call has a **timeout** and **retry with exponential backoff** (max 3; no retry on 4xx except 429).
- Everything is **idempotent**: `messages.idempotency_key`, `actions.idempotency_key`, and at most one live draft per message.
- Failures after retries go to `dead_letters` with payload and error. Nothing is dropped silently.
- LLM calls go through LiteLLM with the tenant's **fallback model** configured.
- All n8n workflows use the global Error Workflow `wf_error_handler`.

### Observability
- `trace_id` is created at ingest and carried through every workflow, service call, LLM call, log line and DB row.
- Every LLM call writes `ai_runs` (model, prompt version, tokens, cost, latency, status, fallback used).
- Logs are structured JSON. Never log raw bodies or document contents. Log IDs.

### Security & privacy
- **No secrets** in code, prompts, pack files or workflow JSON. CI scans for secret patterns.
- Inbound webhooks verify an **HMAC signature** plus a 5-minute timestamp window.
- PII is **redacted before text reaches the LLM**, unless a tool needs that field. `messages.body_redacted` is what the AI sees.
- Files in MinIO are private. Access goes through short-lived pre-signed URLs only.
- `wf_gdpr_delete` anonymizes contacts, nulls message bodies, deletes their files and keeps the audit trail.

### Tenant isolation (three layers: all required)
1. **Composite foreign keys** `(tenant_id, id)`: a row can't reference another tenant's parent row.
2. **Row-level security** on every tenant table. The core service opens each transaction with
   `SELECT set_config('app.tenant_id', $tenant, true)` (transaction-local). No context → zero rows (fail closed).
   Tenant resolution before context exists uses only `resolve_channel_endpoint()`.
3. **Application:** every query still filters by `tenant_id`. Cross-tenant jobs (SLA watchdog, reconciler)
   loop over tenants and set context per tenant. They never use an RLS-bypassing role.

### Database roles (provisioned by `scripts/create_db_users` before migrations)
| Role | Used by | Can | Cannot |
|---|---|---|---|
| bootstrap superuser | `scripts/create_db_users` | Provision the database, extensions, and roles | Is never used by a running service |
| non-superuser schema owner | `scripts/migrate`, seed runner, owner checks | DDL on owned objects | Cannot create roles or bypass RLS on tables owned by other roles |
| `app_core` | core service | Read config; read/insert/update operational tables; insert audit | Change packs/tools/tenant config; UPDATE/DELETE audit; DELETE anything |
| `app_config_loader` | `scripts/load_packs`, admin tools | Write packs, tools, tenant config, knowledge, eval cases | Touch cases/messages/actions/approvals |
| `app_n8n_mock` | n8n mock tool branches | Read/write `demo` schema | Read any engine table |
| `app_reporting` | Metabase | Read `v_*` views | Read raw tables |

**The AI path cannot change its own permissions:** packs, tools, risk levels and approval roles are
read-only to `app_core`. They change only through Git → CI → `load_packs`.

### Database-enforced invariants (proven by `db/tests/safety_checks.sql`)
- Actions start as `proposed`/`pending_approval`. Risk level must match the registry. The tool must be allowed for the case's pack.
- Legal state transitions only. `executing` requires a claim + lease. `unknown_outcome` must be reconciled, never blindly retried.
- **Approval binding:** a request captures the payload hash. Approval fails if the payload changed. Payloads freeze once approval is requested. `approved_by` must be the reviewer who actually approved. The reviewer's role and tenant are checked in the DB.
- `send_reply` can only send an approved draft from the same case. Approved draft text is frozen.
- Audit log is append-only and hash-chained. `verify_audit_chain()` detects edits.

### Known limits (do not claim otherwise)
- A PostgreSQL **superuser or the table owner can bypass triggers and RLS**. Mitigations: services never
  use those accounts; the audit hash chain makes tampering *detectable*; `wf_audit_verify` runs daily
  and **anchors the latest `row_hash` outside the DB** (Slack post + MinIO object-lock bucket), which makes
  it *provable*.
- DB invariants prove the database refuses bad states. They do **not** prove the application, the n8n
  workflows or real integrations are safe. Those need their own tests (§12).
- External APIs without idempotency keys (some CRMs) can still duplicate on a crash. The reconciler
  must search-before-create for those tools.

### AI usage
- All LLM outputs use **structured output against a JSON Schema**. Validate. On failure retry once, then `needs_approval`.
- Prompts are versioned files. Never edit an evaluated prompt; create `vN+1`.
- **All external content is untrusted data, never instructions:** customer messages, form fields, email
  headers, PDF text, OCR output, *and retrieved knowledge chunks*. Wrap it in delimiters in every prompt.
  The model's output is a *proposal* validated against the schema and the policy engine. Nothing the
  model writes can grant a tool, change a threshold or skip an approval.
- Use a cheap, fast model for routing, classification and extraction of short text. Use a stronger model
  for drafting and document extraction. Set this in LiteLLM config, not in code.
- Temperature ≤ 0.2 for classification and extraction.

### Audit
- Every meaningful state change writes `audit_log` (append-only, enforced by DB trigger).

---

## 11. n8n conventions

- Workflow names: `wf_<area>_<purpose>`, e.g. `wf_ingest_email`, `wf_ingest_form`, `wf_case_pipeline`,
  `wf_approval_slack`, `wf_sla_watchdog`, `wf_error_handler`, `wf_gdpr_delete`, `wf_kb_ingest`.
- **Workflows are pack-agnostic.** No pack-specific branches in n8n. Pack differences live in pack files and the core service.
- One responsibility per workflow. Use Execute Workflow for sub-flows. Name every node descriptively.
- Code nodes > 30 lines move into `services/core`.
- Export changed workflows to `/workflows` (credentials stripped) and commit.

---

## 12. Testing & evaluation

- `db/tests/safety_checks.sql` runs in CI against a fresh DB (migrate → seed → tests) and must exit 0.
  Add a check for every new invariant.
- Unit tests for every global and pack rule, the condition language, guardrails and tool contracts (mock and real).
- **Failure-mode tests** (Phase 3 onward): two workers claiming one action; worker killed between the
  external call and `/complete`; external API timeout; duplicate webhook during processing; approval
  arriving after SLA expiry; RLS context missing; mock grant sent to a real branch.
- `services/core/app/engine` coverage ≥ 90%.
- Each pack ships `evals/cases.jsonl`. Target 100+ cases per pack, including typos, multiple intents,
  non-English messages, injection attempts and edge cases. Document cases reference files in `evals/fixtures/`.
- Eval metrics: intent accuracy, decision accuracy, extraction field accuracy (document packs),
  cost per case, p50/p95 latency.
- **Release gates (CI blocks merges that fail these):**
  - intent accuracy ≥ 90% per pack
  - extraction exact-match ≥ 95% on required fields (`invoice_processing`)
  - **zero** `auto` decisions on cases tagged `must_escalate`, `injection` or `money`
  - **zero** tool data leaked on cases tagged `unverified`

---

## 13. Definition of done (every task)

- [ ] Code + tests; `ruff`, `mypy`, `pytest` pass
- [ ] Migrations forward-only and run on an empty DB
- [ ] `db/tests/safety_checks.sql` passes (and has new checks for any new invariant)
- [ ] Packs validate (`scripts/load_packs --check`)
- [ ] Workflows exported, no credentials
- [ ] `trace_id`, `ai_runs`, `audit_log` present for the new path
- [ ] Timeouts, retries and idempotency handled
- [ ] Evals run if prompts, models, rules or tools changed; gates pass
- [ ] Docs updated: runbook entry for new failure modes, ADR for architectural choices

---

## 14. Build phases

Each phase ends with something you can demo.

1. **Foundation (small and verifiable; no AI, no n8n workflows yet):** docker-compose, `.env.example`,
   migration runner (001 + 002), seed runner, `scripts/create_db_users` (one login per role, passwords from env),
   `db/tests/safety_checks.sql` wired into a test command and CI, pack loader + validation for all three packs,
   core service skeleton with `/health` and `/ingest` (tenant resolution, dedupe, RLS context, audit entry).
2. **AI core:** LiteLLM, `/understand` (classify + extract with pack schemas), PII redaction, injection detection,
   `ai_runs` + Langfuse, eval runner + report, first 50 cases per pack.
3. **Pack 1, `ecommerce_support`, end to end with mock data:** n8n ingest + case pipeline + approval workflows,
   RAG, decision pipeline + condition language, drafts with citations, output guardrails, claim/complete/reconcile
   protocol, Slack approvals with SLA watchdog. **Gate before Pack 2:** all failure-mode tests (§12) pass.
   Then switch to real adapters: Shopify dev store + Gmail.
4. **Pack 2, `sales_leads`:** web form + email intake, company enrichment, lead scoring rules, HubSpot contact/deal,
   Cal.com booking link, sales Slack alert.
5. **Pack 3, `invoice_processing`:** PDF intake, extraction with per-field confidence, vendor/PO matching,
   duplicate check, QuickBooks/Xero sandbox posting, finance approval for payments.
6. **Hardening + packaging:** Metabase dashboards per pack, injection test suite, budgets, VPS deploy with Caddy,
   CI gates, runbook, architecture diagram, demo video script, one case study per pack.

---

## 15. Things you must NOT do

- Do not let the LLM execute any tool directly. It proposes; the engine authorizes; n8n executes with a grant.
- Do not give n8n, or any service, a connection to engine tables other than through the core API.
- Do not weaken, drop or work around a database constraint, trigger, policy or grant to make code work.
  If one blocks you, stop and explain why.
- Do not use the owner/superuser DB account from any running service.
- Do not send any outbound message without a `drafts` row and a `policy_decisions` row.
- Do not put pack-specific logic in engine code or n8n workflows. Put it in the pack.
- Do not hard-code tenant values. Read `tenant_settings` and `tenant_packs.config`.
- Do not edit applied migrations, released pack versions or evaluated prompts. Add new versions.
- Do not log, print or commit secrets, raw PII or document contents.
- Do not add a dependency, service, table or tool without a short ADR in `docs/adr/`.
- Do not "fix" a failing eval by editing the expected answer unless the case is genuinely wrong. Say so in the commit.
