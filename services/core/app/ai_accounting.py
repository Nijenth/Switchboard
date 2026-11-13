from decimal import Decimal
from time import monotonic
from typing import Any
from uuid import UUID

from psycopg import Connection
from psycopg_pool import ConnectionPool

from app.adapters.ai import OfflineGateway, Provider
from app.understanding import Step, StopAttempts, validate_output


class Accounting:
    def __init__(
        self,
        pool: ConnectionPool[Connection],
        gateway: OfflineGateway,
        tenant: UUID,
        trace: UUID,
        redacted: str,
        primary: Provider,
        case: UUID | None = None,
        message: UUID | None = None,
    ) -> None:
        self.pool, self.gateway = pool, gateway
        self.tenant, self.trace = tenant, trace
        self.case, self.message = case, message
        self.redacted, self.primary = redacted, primary
        self.cost = Decimal(0)
        self.latency_ms = 0
        self.blocked = False
        self.cost_unknown = False
        self.runs: dict[Step, UUID] = {}

    def attempt(
        self,
        step: Step,
        provider: Provider,
        version: str,
        instructions: str,
        schema: dict[str, Any],
    ) -> dict[str, Any] | None:
        prepared = self.gateway.prepare(provider, instructions, self.redacted, schema)
        if prepared is None:
            raise StopAttempts
        reservation = prepared.reservation
        with self.pool.connection() as conn, conn.transaction():
            self.scope(conn)
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"switchboard.ai_budget:{self.tenant}",),
            )
            remaining = conn.execute(
                """
                SELECT s.monthly_ai_budget_usd - COALESCE((
                    SELECT sum(CASE WHEN r.accounting_pending THEN r.reserved_cost_usd ELSE r.cost_usd END)
                    FROM public.ai_runs r WHERE r.tenant_id=s.tenant_id
                      AND r.created_at >= date_trunc('month', statement_timestamp(), 'UTC')
                ),0) FROM public.tenant_settings s WHERE s.tenant_id=%s
                """,
                (self.tenant,),
            ).fetchone()
            allowed = remaining is not None and remaining[0] >= reservation
            inserted = conn.execute(
                """
                INSERT INTO public.ai_runs (
                    tenant_id,trace_id,case_id,message_id,step,provider,model,prompt_version,
                    used_fallback,status,reserved_cost_usd,accounting_pending
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
                """,
                (
                    self.tenant,
                    self.trace,
                    self.case,
                    self.message,
                    step,
                    provider,
                    f"switchboard-{provider}",
                    version,
                    provider != self.primary,
                    "error" if allowed else "budget_blocked",
                    reservation if allowed else 0,
                    allowed,
                ),
            ).fetchone()
            assert inserted is not None
            run_id = inserted[0]
        if not allowed:
            self.blocked = True
            raise StopAttempts

        start = monotonic()
        completion = self.gateway.call(prepared)
        latency = round((monotonic() - start) * 1000)
        result = (
            validate_output(completion.output, schema) if completion.output is not None else None
        )
        status = (
            "schema_invalid"
            if completion.status == "success" and result is None
            else completion.status
        )
        with self.pool.connection() as conn, conn.transaction():
            self.scope(conn)
            conn.execute(
                """
                UPDATE public.ai_runs SET model=%s, status=%s, input_tokens=%s, output_tokens=%s,
                  latency_ms=%s, cost_usd=%s, accounting_pending=%s
                WHERE tenant_id=%s AND id=%s
                """,
                (
                    completion.model,
                    status,
                    completion.input_tokens,
                    completion.output_tokens,
                    latency,
                    completion.cost or 0,
                    completion.cost is None,
                    self.tenant,
                    run_id,
                ),
            )
        self.cost += completion.cost or Decimal(0)
        self.cost_unknown |= completion.cost is None
        self.latency_ms += latency
        if result is not None:
            self.runs[step] = run_id
        if not completion.retryable:
            raise StopAttempts
        return result

    def scope(self, conn: Connection[Any]) -> None:
        set_scope(conn, self.tenant)


def set_scope(conn: Connection[Any], tenant: UUID) -> None:
    conn.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant),))
