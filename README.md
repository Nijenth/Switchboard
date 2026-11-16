# Switchboard

[![Phase 1 checks](https://github.com/Nijenth/Switchboard/actions/workflows/phase1.yml/badge.svg?branch=main)](https://github.com/Nijenth/Switchboard/actions/workflows/phase1.yml)

A multi-tenant AI operations engine. It takes incoming work (emails, chats, web forms), understands it,
takes action in business tools under strict rules, asks a human when it should, and records every decision.

The engine is generic. Each business use case is a **pack**: a folder of configuration (intents, extraction
schemas, allowed tools, business rules, prompts, knowledge, eval cases). Adding a use case means adding a
pack, not changing the engine.

> **Status: portfolio project, about half built.** Pack 1 (e-commerce customer support) works end to end
> on a mock stack. AI quality is **built, not measured**: every test runs against a fake model, and no
> real-model evaluation has been run yet. See [What is not done](#what-is-not-done).

## What it does (Pack 1, `ecommerce_support`)

A customer emails the fictional retailer Northwind Apparel. Switchboard:

1. **Ingests** the email once (duplicate deliveries are ignored) and opens a case.
2. **Guards the input:** redacts personal data before any model sees it (rules-based, known gaps) and
   flags prompt-injection attempts.
3. **Understands** the request: intent and extracted fields, validated against the pack's JSON Schemas.
4. **Checks identity** before looking anything up: an order is only read for the customer who owns it.
5. **Decides** with a policy engine: global safety rules, then pack rules, then tenant rules.
   Results are `auto`, `needs_approval`, `escalate` or `ignore`.
6. **Acts** through registered tools. Money (refunds) always needs a human with the right role.
7. **Drafts** replies with citations and checks them before anything goes out.
8. **Records** every step in a hash-chained, append-only audit log.

## Design highlights

- **The database enforces the safety rules, not just the code.** Row-level security isolates tenants
  (no tenant context means zero rows). Triggers block illegal state changes, approvals are role-checked
  and frozen to the exact payload, and the audit log cannot be edited without detection.
- **Crash-safe execution.** An action is claimed with a lease and a single-use grant. If a worker dies
  mid-call, the action becomes `unknown_outcome`, which the database refuses to retry blindly; a
  reconciler checks the external system first. Two workers can never both run one action.
- **n8n does the plumbing; a FastAPI core service decides.** n8n handles triggers, sequencing and
  connectors. It never touches engine tables and never makes business decisions.
- **The model only proposes.** Nothing a model writes can grant a tool, change a threshold or skip an
  approval.
- **Real provider credentials live in a separate connector gateway** (`services/connectors`), never in
  n8n. It is built and tested against stubs but deliberately **switched off** until sandbox testing.

## Architecture

```
email · web chat · web form · API
        │  signed webhooks
        ▼
n8n (queue mode)  ──calls──▶  core service (FastAPI)  ──▶  PostgreSQL + pgvector
 triggers, retries,            ingest · guardrails ·         row-level security,
 connectors, Slack             understand · retrieve ·       audit chain, invariants
 approval buttons              decide · draft · authorize
                                     │
                                     ▼
                               LiteLLM (model gateway)
```

| Part | Technology |
|---|---|
| Orchestration | n8n (self-hosted, Docker) |
| Core service | Python 3.12, FastAPI, Pydantic v2 |
| Database | PostgreSQL 16 with pgvector, row-level security |
| Model gateway | LiteLLM (fake model server offline) |
| Tests and CI | pytest, ruff, mypy, SQL safety checks, GitHub Actions |

Architecture decisions are recorded in [`docs/adr/`](docs/adr/).

## Try it

You need Docker Desktop, Python 3.12, and Bash (Git Bash on Windows).

```bash
python -m pip install -r services/core/requirements-dev.txt -r scripts/requirements-packs.txt
python scripts/load_packs --check          # validate every pack
bash scripts/test_core.sh --offline-n8n    # full pipeline: disposable DB, fake model, n8n workflows
```

To click through the demo in a browser (refund approval, a prompt-injection email, free text):

```bash
bash scripts/demo_live up --console        # then open http://127.0.0.1:8780
bash scripts/demo_live down                # stop and remove everything it created
```

Everything binds to `127.0.0.1`, uses mock adapters and a fake model, and sends no real email or money.
See [`docs/demo.md`](docs/demo.md) for the scripted walkthrough and [`docs/runbook.md`](docs/runbook.md)
for operations notes.

## Testing

- **Database invariants:** `db/tests/*.sql` run against a fresh database on every CI run and prove the
  database refuses bad states (cross-tenant writes, unapproved payouts, double execution, audit edits).
- **Core:** `scripts/test_core.sh` in three modes: plain (what CI runs), `--offline-ai` (fake model and
  eval runner), `--offline-n8n` (adds the real n8n workflows, crash simulations and the live-demo lifecycle).
- **Evals:** 110 Pack 1 cases (typos, multiple intents, non-English, injection attempts, money cases),
  each run twice. Release gates: zero `auto` decisions on cases tagged `must_escalate`, `injection` or
  `money`, and zero tool data leaked on unverified cases.

Offline eval numbers measure the **policy and safety gates given fixture intents**, not model quality.

## What is not done

- **No real-model evaluation yet.** Intent accuracy and cost are unmeasured.
- **PII redaction is rules-based** and misses names and addresses outside its patterns. Not ready for
  real customer data.
- **Real Shopify and Gmail are not connected.** The gateway is built; enabling it has an open checklist
  in [`docs/review-queue.md`](docs/review-queue.md).
- **Packs 2 and 3** (sales leads, invoice processing) exist only as validated configuration.
- **Local demo on Windows:** Docker Desktop's host forwarding occasionally drops n8n→core connections
  ([ADR 0033](docs/adr/0033-console-timeout.md)). Linux CI is not affected.
- **Not deployed:** dashboards, budgets and the VPS deploy are still to come.

## Repository layout

```
db/            migrations, seeds, SQL safety checks
packs/         ecommerce_support, sales_leads, invoice_processing
services/core/ FastAPI core service, engine, guardrails, test harness
services/connectors/  connector gateway for real providers (disabled)
workflows/     exported n8n workflows (no credentials)
scripts/       migrate, seed, load_packs, test and demo runners
docs/          ADRs, runbook, demo script, case study
```

[`AGENTS.md`](AGENTS.md) is the full specification the project is built against.
