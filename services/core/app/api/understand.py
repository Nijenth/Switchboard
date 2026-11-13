from collections.abc import Callable
from hmac import compare_digest
from pathlib import PurePosixPath
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException
from psycopg import Connection, Error
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool, PoolTimeout, TooManyRequests
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from app.adapters.ai import OfflineGateway, Provider
from app.adapters.ingest import audit
from app.ai_accounting import Accounting, set_scope
from app.understanding import understand

DEFAULT_RERUN_KEY = UUID(int=0)


class UnderstandInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: UUID
    message_id: UUID
    rerun_key: UUID | None = None


class Credential(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: UUID
    token: str = Field(pattern=r"^[0-9a-f]{64}$", repr=False)


def understand_router(
    pool: ConnectionPool[Connection],
    config: str | None,
    base: str | None,
    primary: Provider,
    bearer_token: Callable[[str | None], str],
) -> APIRouter:
    router = APIRouter()
    try:
        credentials = TypeAdapter(list[Credential]).validate_json(config or "[]")
    except ValidationError:
        raise ValueError("Invalid understand credentials") from None
    if len({entry.token for entry in credentials}) != len(credentials):
        raise ValueError("Understand credentials must be unique")
    gateway = OfflineGateway(base) if base else None

    @router.post("/understand")
    def endpoint(
        payload: UnderstandInput, authorization: Annotated[str | None, Header()] = None
    ) -> dict[str, Any]:
        token = bearer_token(authorization)
        tenant = next(
            (entry.tenant_id for entry in credentials if compare_digest(token, entry.token)), None
        )
        if tenant is None:
            raise HTTPException(403, "Understand credential is not authorized")
        if payload.rerun_key == DEFAULT_RERUN_KEY:
            raise HTTPException(422, "Rerun key must be a nonzero UUID")
        rerun_key = payload.rerun_key or DEFAULT_RERUN_KEY
        try:
            with pool.connection() as conn, conn.transaction():
                set_scope(conn, tenant)
                row = conn.execute(
                    """
                    SELECT m.trace_id,m.body_redacted,p.definition,p.id
                    FROM public.messages m
                    JOIN public.cases c ON c.tenant_id=m.tenant_id AND c.id=m.case_id
                    LEFT JOIN public.tenant_packs tp ON tp.tenant_id=c.tenant_id AND tp.id=c.tenant_pack_id
                    LEFT JOIN public.packs p ON p.id=c.pack_id AND tp.enabled
                    WHERE m.tenant_id=%s AND c.id=%s AND m.id=%s AND m.direction='inbound'
                    """,
                    (tenant, payload.case_id, payload.message_id),
                ).fetchone()
                if row is None:
                    raise HTTPException(404, "Message unavailable")
                trace, redacted, pack, pack_id = row
                if redacted is None or pack is None:
                    return {
                        "trace_id": trace,
                        "decision": "needs_approval",
                        "reason": "input_unavailable",
                    }
                request_key = (tenant, payload.message_id, pack_id, rerun_key)
                cached = conn.execute(
                    """
                    SELECT result FROM public.understand_requests
                    WHERE tenant_id=%s AND message_id=%s AND pack_id=%s AND rerun_key=%s
                    """,
                    request_key,
                ).fetchone()
                if cached is not None:
                    if cached[0] is None:
                        raise HTTPException(409, "Understanding already claimed")
                    result_json: dict[str, Any] = cached[0]
                    return result_json
                if gateway is None:
                    raise HTTPException(503, "Offline gateway unavailable")
                claimed = conn.execute(
                    """
                    INSERT INTO public.understand_requests
                        (tenant_id,message_id,pack_id,rerun_key,case_id)
                    VALUES (%s,%s,%s,%s,%s)
                    ON CONFLICT DO NOTHING RETURNING rerun_key
                    """,
                    (*request_key, payload.case_id),
                ).fetchone()
                if claimed is None:
                    cached = conn.execute(
                        """
                        SELECT result FROM public.understand_requests
                        WHERE tenant_id=%s AND message_id=%s AND pack_id=%s AND rerun_key=%s
                        """,
                        request_key,
                    ).fetchone()
                    if cached is None or cached[0] is None:
                        raise HTTPException(409, "Understanding already claimed")
                    replay: dict[str, Any] = cached[0]
                    return replay
            accounting = Accounting(
                pool,
                gateway,
                tenant,
                trace,
                redacted,
                primary,
                payload.case_id,
                payload.message_id,
            )
            result = understand(pack, redacted, primary, accounting.attempt)
            response = {
                "trace_id": str(trace),
                "decision": result.decision,
                "reason": "budget_blocked" if accounting.blocked else result.reason,
                "intent": result.intent,
                "confidence": result.confidence,
                "language": result.language,
                "extraction": result.extraction,
                "field_confidence": result.field_confidence,
                "flags": list(result.flags),
            }
            with pool.connection() as conn, conn.transaction():
                set_scope(conn, tenant)
                # A local guardrail escalated before any model classification ran.
                if result.decision == "escalate" and result.intent is None:
                    conn.execute(
                        """
                        INSERT INTO public.classifications
                            (tenant_id,case_id,message_id,pack_id,intent_key,
                             method,priority,language,confidence,flags)
                        VALUES (%s,%s,%s,%s,'system_escalation','rules','urgent',%s,1,%s)
                        """,
                        (
                            tenant,
                            payload.case_id,
                            payload.message_id,
                            pack_id,
                            result.language,
                            [result.reason],
                        ),
                    )
                if result.intent is not None:
                    priority = next(
                        intent["default_priority"]
                        for intent in pack["intents"]
                        if intent["key"] == result.intent
                    )
                    conn.execute(
                        """
                        INSERT INTO public.classifications
                            (tenant_id,case_id,message_id,pack_id,intent_key,ai_run_id,
                             method,priority,language,confidence,flags)
                        VALUES (%s,%s,%s,%s,%s,%s,'llm',%s,%s,%s,%s)
                        """,
                        (
                            tenant,
                            payload.case_id,
                            payload.message_id,
                            pack_id,
                            result.intent,
                            accounting.runs["classify"],
                            priority,
                            result.language,
                            result.confidence,
                            list(result.flags),
                        ),
                    )
                if extraction_run := accounting.runs.get("extract"):
                    schema_file = PurePosixPath(pack["extraction"]["schema"]).stem
                    schema_key, _, schema_version = schema_file.rpartition(".v")
                    if schema_key and schema_version.isdecimal():
                        conn.execute(
                            """
                            INSERT INTO public.extractions (
                                tenant_id,case_id,message_id,pack_id,schema_key,schema_version,
                                data,field_confidence,status,ai_run_id
                            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                            """,
                            (
                                tenant,
                                payload.case_id,
                                payload.message_id,
                                pack_id,
                                schema_key,
                                int(schema_version),
                                Jsonb(result.extraction),
                                Jsonb(result.field_confidence),
                                "needs_review"
                                if result.reason == "extraction_confidence"
                                else "validated",
                                extraction_run,
                            ),
                        )
                audit(conn, tenant, trace, "message.understood", "message", payload.message_id)
                conn.execute(
                    """
                    UPDATE public.understand_requests SET result=%s
                    WHERE tenant_id=%s AND message_id=%s AND pack_id=%s AND rerun_key=%s
                    """,
                    (Jsonb(response), *request_key),
                )
            return response
        except (Error, PoolTimeout, TooManyRequests):
            raise HTTPException(503, "Understand unavailable") from None

    return router
