# Switchboard runbook

## Run the database safety suite

Copy `.env.example` to `.env` and fill in the five local test passwords. Use single-line, distinct random values. Keep `.env` local.

From the repository root, with Docker Desktop running in Linux container mode, run:

```powershell
docker compose --env-file .env --profile test run --rm --no-deps db-check
```

The command creates a new PostgreSQL 16 cluster on temporary storage with no network and no published ports. It loads the migrations and demo seed, creates the four application logins, and runs `db/tests/safety_checks.sql` through the same private socket. Compose removes the container when the command exits.

On a nonzero exit, use the first reported failure to identify the step that stopped. A missing password fails before PostgreSQL starts. A SQL error stops migration, seed, or role setup immediately. The safety suite prints each check and raises an error if any check fails. Each invocation starts with a fresh cluster, so a rerun after correcting the cause needs no reset.
