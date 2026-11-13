# Switchboard runbook

## Run the database safety suite

Copy `.env.example` to `.env` and fill in the six local test passwords. Use single-line, distinct random values. Keep `.env` local.

From the repository root, with Docker Desktop running in Linux container mode, run:

```powershell
docker compose --env-file .env --profile test run --rm --no-deps db-check
```

The command creates a new PostgreSQL 16 cluster on temporary storage with no network and no published ports. The bootstrap superuser creates the database, roles, and extensions. The non-superuser schema owner applies the migrations and demo seed, then runs `db/tests/safety_checks.sql`. The core, mock, and reporting checks each open a separate connection with the respective login. Every connection uses the same private socket. Compose removes the container when the command exits.

On a nonzero exit, use the first reported failure to identify the step that stopped. A missing password fails before PostgreSQL starts. A SQL error stops migration, seed, or role setup immediately. The safety suite prints each check and raises an error if any check fails. Each invocation starts with a fresh cluster, so a rerun after correcting the cause needs no reset.
