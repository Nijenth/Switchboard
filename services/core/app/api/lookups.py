"""Propose the pack's declared read-only lookups for a message (ADR 0019)."""

from collections.abc import Callable
from hmac import compare_digest
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException
from jsonschema import Draft202012Validator
from psycopg import Connection, Error, IntegrityError
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool, PoolTimeout, TooManyRequests
from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError
from referencing import Registry

from app.adapters.ingest import audit
from app.ai_accounting import set_scope
from app.api.understand import Credential


class LookupInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: UUID
    message_id: UUID


def resolve(source: str, extraction: dict[str, Any], sender: str | None) -> Any:
    if source == "message.sender_address":
        return sender
    if source.startswith("extraction."):
        return extraction.get(source.removeprefix("extraction."))
    return None


def propose_lookups(conn: Connection[Any], tenant: UUID, request: LookupInput) -> dict[str, Any]:
    """Create missing lookup actions and report which are pending, done or waiting.

    Callers run the pending actions through the execution protocol and call again until
    nothing is pending: a tool that needs a verified identity is proposed only after an
    identity check has verified the case.
    """
    set_scope(conn, tenant)
    row = conn.execute(
        """
        SELECT m.trace_id,m.sender_address,c.identity_verified,c.pack_id,p.definition,tp.config
        FROM public.messages m
        JOIN public.cases c ON (c.tenant_id,c.id)=(m.tenant_id,m.case_id)
        JOIN public.tenant_packs tp ON (tp.tenant_id,tp.id)=(c.tenant_id,c.tenant_pack_id)
        JOIN public.packs p ON p.id=c.pack_id AND p.key=tp.pack_key
        WHERE m.tenant_id=%s AND c.id=%s AND m.id=%s AND m.direction='inbound' AND tp.enabled
        FOR UPDATE OF c
        """,
        (tenant, request.case_id, request.message_id),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Lookup message unavailable")
    trace, sender, verified, pack_id, pack, enrollment = row
    extracted = conn.execute(
        """
        SELECT data FROM public.extractions
        WHERE tenant_id=%s AND case_id=%s AND message_id=%s AND status='validated'
        ORDER BY created_at DESC,id DESC LIMIT 1
        """,
        (tenant, request.case_id, request.message_id),
    ).fetchone()
    extraction = extracted[0] if extracted and isinstance(extracted[0], dict) else {}
    tools = {
        key: (requires_identity, order_scoped, schema)
        for key, requires_identity, order_scoped, schema in conn.execute(
            """
            SELECT t.key,t.requires_verified_identity,public.is_order_scoped_tool(t),t.input_schema
            FROM public.pack_tools pt JOIN public.tools t ON t.key=pt.tool_key AND t.active
            WHERE pt.pack_id=%s AND t.risk_level='read'
            """,
            (pack_id,),
        )
    }
    adapters = (enrollment or {}).get("adapters", {}) if isinstance(enrollment, dict) else {}
    lookups = pack.get("lookups", []) if isinstance(pack, dict) else []
    pending: list[dict[str, Any]] = []
    completed: list[str] = []
    waiting: list[dict[str, str]] = []
    for entry in lookups if isinstance(lookups, list) else []:
        key = entry.get("tool") if isinstance(entry, dict) else None
        if not isinstance(key, str) or key not in tools:
            waiting.append({"tool_key": str(key), "reason": "tool_unavailable"})
            continue
        requires_identity, order_scoped, schema = tools[key]
        sources = entry.get("input", {})
        payload = {name: resolve(source, extraction, sender) for name, source in sources.items()}
        if (
            not payload
            or any(value is None for value in payload.values())
            or not isinstance(schema, dict)
            or not Draft202012Validator(schema, registry=Registry()).is_valid(payload)
        ):
            waiting.append({"tool_key": key, "reason": "input_unavailable"})
            continue
        order_number = payload.get("order_number")
        if requires_identity and not (
            order_scoped
            and verified
            and isinstance(order_number, str)
            and conn.execute(
                "SELECT public.order_identity_is_verified(%s,%s,%s,%s)",
                (tenant, request.case_id, request.message_id, order_number),
            ).fetchone()
            == (True,)
        ):
            waiting.append({"tool_key": key, "reason": "identity_unverified"})
            continue
        idempotency_key = f"lookup:{request.message_id}:{key}"
        existing = conn.execute(
            "SELECT id,status FROM public.actions WHERE tenant_id=%s AND idempotency_key=%s",
            (tenant, idempotency_key),
        ).fetchone()
        if existing is not None:
            action_id, status = existing
            if status == "executed":
                completed.append(key)
            elif status in ("proposed", "executing"):
                pending.append({"action_id": action_id, "tool_key": key})
            else:
                waiting.append({"tool_key": key, "reason": status})
            continue
        adapter = adapters.get(key, "mock") if isinstance(adapters, dict) else "mock"
        if adapter != "mock":
            waiting.append({"tool_key": key, "reason": "real_adapter_unavailable"})
            continue
        action = conn.execute(
            """
            INSERT INTO public.actions (tenant_id,case_id,message_id,trace_id,idempotency_key,
                                        tool_key,risk_level,input,status,adapter)
            VALUES (%s,%s,%s,%s,%s,%s,'read',%s,'proposed','mock') RETURNING id
            """,
            (
                tenant,
                request.case_id,
                request.message_id,
                trace,
                idempotency_key,
                key,
                Jsonb(payload),
            ),
        ).fetchone()
        assert action is not None
        audit(conn, tenant, trace, "action.proposed", "action", action[0])
        pending.append({"action_id": action[0], "tool_key": key})
    return {"trace_id": trace, "pending": pending, "completed": completed, "waiting": waiting}


def lookup_router(
    pool: ConnectionPool[Connection], config: str | None, bearer_token: Callable[[str | None], str]
) -> APIRouter:
    router = APIRouter()
    try:
        credentials = TypeAdapter(list[Credential]).validate_json(config or "[]")
    except ValidationError:
        raise ValueError("Invalid lookup credentials") from None
    if len({entry.token for entry in credentials}) != len(credentials):
        raise ValueError("Lookup credentials must be unique")

    @router.post("/lookups")
    def endpoint(
        request: LookupInput, authorization: Annotated[str | None, Header()] = None
    ) -> dict[str, Any]:
        token = bearer_token(authorization)
        tenant = next(
            (entry.tenant_id for entry in credentials if compare_digest(token, entry.token)), None
        )
        if tenant is None:
            raise HTTPException(403, "Lookup credential is not authorized")
        try:
            with pool.connection() as conn, conn.transaction():
                return propose_lookups(conn, tenant, request)
        except IntegrityError:
            raise HTTPException(409, "Lookup could not be proposed") from None
        except (Error, PoolTimeout, TooManyRequests):
            raise HTTPException(503, "Lookups unavailable") from None

    return router
