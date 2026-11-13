# Codex prompt — Phase 1

Paste everything below the line into Codex.

---

Read these files completely before making any change:
`AGENTS.md`, `db/migrations/001_init.sql`, `db/migrations/002_security_hardening.sql`,
`db/seeds/001_demo_seed.sql`, `db/tests/safety_checks.sql`.

Your task is to implement **Phase 1 only**, exactly as defined in `AGENTS.md` §14.

## Requirements

1. Implement `docker-compose.yml` (Postgres 16 + pgvector, Redis, MinIO, n8n main + worker in queue mode
   with its own `n8n` database, core service), `.env.example`, a migration runner, a seed runner, and
   `scripts/create_db_users` that creates one login user per DB role (`app_core`, `app_config_loader`,
   `app_n8n_mock`, `app_reporting`) with passwords from environment variables.
2. Implement the pack loader (`scripts/load_packs`) with validation, and create the three pack folders
   (`ecommerce_support`, `sales_leads`, `invoice_processing`) so they match the seeded packs.
3. Implement the core service skeleton: `/health` (checks db, redis, minio) and `/ingest` (canonical input
   → tenant via `resolve_channel_endpoint()` → `set_config('app.tenant_id', …, true)` → case + message,
   idempotent → audit entry). The core service connects **only** as `app_core`.
4. Preserve the engine-plus-packs architecture and the n8n/core boundary in `AGENTS.md` §2.1.
5. Do not implement features assigned to later phases: no LLM calls, no n8n workflows, no real integrations.
6. Do not weaken, remove or bypass any database constraint, trigger, RLS policy or grant.
7. Validate pack keys, intents, tool references, risk levels, approval roles, file references and
   rule syntax (the condition language in `AGENTS.md` §5.4). Reject malformed, ambiguous or unsupported
   pack configurations with clear errors that name the file and field.
8. Ensure mock configuration cannot reach real external services (no real credentials in Phase 1 at all).
9. Use least-privilege DB credentials. Never commit secrets. `.env` must be git-ignored.
10. Make database initialization reproducible from a clean environment with one command
    (e.g. `make reset-db`: drop → migrate 001, 002 → seed → load packs).
11. Add automated tests: successful setup; invalid pack configurations (at least 8 distinct failure cases);
    `/ingest` duplicate handling; `/ingest` with an unknown endpoint; RLS (`app_core` cannot read
    another tenant's rows through the service).

## Verification (run these and report actual output)

- Run migrations + seed against a fresh test database.
- Run `db/tests/safety_checks.sql`; it must exit 0 and report 55 passed, 0 failed.
- Run the pack loader in check mode against valid packs and your invalid fixtures.
- Run the test suite.
- Show that a failed migration or seed makes the init command exit non-zero (no false "success").
- Confirm no external service or real customer data is contacted.

## Constraints

- Do not assume the system is secure because the safety tests pass. They cover DB invariants only.
- Do not modify `AGENTS.md` or the existing SQL files unless a real defect blocks Phase 1. If so,
  **stop and explain the defect and the proposed change before making it**. Any schema change must be
  a new migration (`003_…`), never an edit to 001 or 002.
- If something in `AGENTS.md` is ambiguous, choose the stricter interpretation and note it.

## Final report

- Files created or modified
- How the pieces fit together (short)
- Commands used, with exit codes
- Test results, including any failures
- Security assumptions and remaining risks
- What is complete in Phase 1, and what remains for Phase 2

Do not claim production readiness.
