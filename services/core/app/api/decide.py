from collections.abc import Callable
from dataclasses import asdict
from hmac import compare_digest
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException
from psycopg import Connection, Error
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool, PoolTimeout, TooManyRequests
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from app.adapters.ingest import audit
from app.ai_accounting import set_scope
from app.api.understand import Credential
from app.engine.policy import decide

ToolKey = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{2,60}$")]


class DecideInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: UUID
    message_id: UUID
    proposed_tools: list[ToolKey] = Field(default_factory=list, max_length=32)


def publish(conn: Connection[Any], tenant: UUID, payload: DecideInput) -> dict[str, Any]:
    set_scope(conn, tenant)
    scope = (tenant, payload.case_id)
    row = conn.execute(
        """
        SELECT c.pack_id,c.policy_revision,c.channel,c.status,c.priority,c.identity_verified,
               t.slug,p.key,p.version,p.definition,tp.config,
               co.id,co.kind,co.locale
        FROM public.cases c
        JOIN public.tenant_packs tp ON (tp.tenant_id,tp.id)=(c.tenant_id,c.tenant_pack_id)
        JOIN public.packs p ON p.id=c.pack_id
        JOIN public.tenants t ON t.id=c.tenant_id
        LEFT JOIN public.contacts co ON (co.tenant_id,co.id)=(c.tenant_id,c.contact_id)
        WHERE c.tenant_id=%s AND c.id=%s AND tp.enabled
        FOR UPDATE OF c
        """,
        scope,
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Decision case unavailable")
    (
        pack_id,
        revision,
        channel,
        status,
        priority,
        verified,
        slug,
        pack_key,
        version,
        pack,
        enrollment,
        contact_id,
        contact_kind,
        contact_locale,
    ) = row
    latest = conn.execute(
        """
        SELECT id,trace_id FROM public.messages
        WHERE tenant_id=%s AND case_id=%s AND direction='inbound'
        ORDER BY created_at DESC,id DESC LIMIT 1
        """,
        scope,
    ).fetchone()
    if latest is None or latest[0] != payload.message_id:
        raise HTTPException(409, "Decision requires the latest inbound message")
    trace = latest[1]
    message_scope = (*scope, payload.message_id, pack_id)
    classification = conn.execute(
        """
        SELECT id,intent_key,confidence,priority,language,sentiment,flags,created_at
        FROM public.classifications
        WHERE tenant_id=%s AND case_id=%s AND message_id=%s AND pack_id=%s
        ORDER BY created_at DESC,id DESC LIMIT 1
        """,
        message_scope,
    ).fetchone()
    flags = conn.execute(
        """
        SELECT DISTINCT f FROM public.classifications c CROSS JOIN LATERAL unnest(c.flags) AS f
        WHERE c.tenant_id=%s AND c.case_id=%s ORDER BY f
        """,
        scope,
    ).fetchall()
    extracted = conn.execute(
        """
        SELECT id,data,field_confidence,status,created_at FROM public.extractions
        WHERE tenant_id=%s AND case_id=%s AND message_id=%s AND pack_id=%s
        ORDER BY created_at DESC,id DESC LIMIT 1
        """,
        message_scope,
    ).fetchone()
    budget = conn.execute(
        """
        SELECT s.monthly_ai_budget_usd <= COALESCE((
            SELECT sum(CASE WHEN r.accounting_pending THEN r.reserved_cost_usd ELSE r.cost_usd END)
            FROM public.ai_runs r WHERE r.tenant_id=s.tenant_id
              AND r.created_at >= date_trunc('month', statement_timestamp(), 'UTC')
        ),0) FROM public.tenant_settings s WHERE s.tenant_id=%s
        """,
        (tenant,),
    ).fetchone()
    blocked_attempt = conn.execute(
        """
        SELECT status,step,created_at FROM public.ai_runs
        WHERE tenant_id=%s AND case_id=%s AND message_id=%s
        ORDER BY created_at DESC,id DESC LIMIT 1
        """,
        message_scope[:3],
    ).fetchone()
    tools = {
        key: {
            "risk_level": risk,
            "requires_verified_identity": identity,
            "approval_role": role,
            "check_fields": list(schema.get("properties", {})),
        }
        for key, risk, identity, role, schema in conn.execute(
            """
            SELECT t.key,t.risk_level,t.requires_verified_identity,pt.approval_role,t.output_schema
            FROM public.pack_tools pt JOIN public.tools t ON t.key=pt.tool_key
            WHERE pt.pack_id=%s
            """,
            (pack_id,),
        )
    }
    proposed = set(payload.proposed_tools)
    if proposed - tools.keys():
        raise HTTPException(422, "Tool is outside the pinned pack")
    labels = ("id", "intent_key", "confidence", "priority", "language", "sentiment", "flags")
    label = dict(zip(labels, classification[:-1], strict=True)) if classification else {}
    if classification:
        label["id"] = str(label["id"])
        label["confidence"] = float(label["confidence"])
        if (
            blocked_attempt
            and blocked_attempt[1] == "classify"
            and blocked_attempt[0] != "success"
            and blocked_attempt[2] >= classification[-1]
        ):
            label["confidence"] = None
    label["flags"] = [flag for (flag,) in flags]
    context = {
        "case": {
            "id": str(payload.case_id),
            "channel": channel,
            "status": status,
            "priority": priority,
            "identity_verified": verified,
        },
        "tenant": {"id": str(tenant), "slug": slug},
        "contact": {"id": str(contact_id), "kind": contact_kind, "locale": contact_locale}
        if contact_id
        else {},
        "classification": label,
        "extraction": extracted[1] if extracted else {},
        "field_confidence": extracted[2] if extracted else {},
        "extraction_valid": (
            extracted is not None
            and classification is not None
            and extracted[3] == "validated"
            and extracted[4] >= classification[-1]
        ),
        "checks": {},
    }
    budget_blocked = (
        budget is None
        or budget[0]
        or (blocked_attempt is not None and blocked_attempt[0] == "budget_blocked")
    )
    overrides = enrollment.get("rules", []) if isinstance(enrollment, dict) else None
    decision = decide(pack, context, tools, proposed, overrides, budget_blocked=budget_blocked)
    result = {**asdict(decision), "allowed_tools": list(decision.allowed_tools)}
    inputs = {
        **context,
        "extraction_id": str(extracted[0]) if extracted else None,
        "proposed_tools": sorted(proposed),
        "tool_policy": tools,
        "budget_blocked": budget_blocked,
        "citations_verified": False,
        "output_guardrails_passed": False,
        "tenant_rules": overrides,
        "result": result,
    }
    policy_version = f"engine:2+{pack_key}:{version}"
    previous = conn.execute(
        """
        SELECT id,inputs FROM public.policy_decisions
        WHERE tenant_id=%s AND case_id=%s AND message_id=%s AND revision=%s AND policy_version=%s
        """,
        (*message_scope[:3], revision, policy_version),
    ).fetchone()
    if previous is not None and previous[1] == inputs:
        return {"decision_id": previous[0], "revision": revision, "trace_id": trace, **result}
    published = conn.execute(
        """
        INSERT INTO public.policy_decisions (
            tenant_id,case_id,message_id,pack_id,trace_id,decision,rule_id,rule_source,
            reason,required_role,allowed_tools,policy_version,inputs
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id,revision
        """,
        (
            *message_scope,
            trace,
            decision.decision,
            decision.rule_id,
            decision.rule_source,
            decision.reason,
            decision.required_role,
            list(decision.allowed_tools),
            policy_version,
            Jsonb(inputs),
        ),
    ).fetchone()
    assert published is not None
    decision_id, revision = published
    audit(conn, tenant, trace, "policy.decided", "policy_decision", decision_id)
    return {"decision_id": decision_id, "revision": revision, "trace_id": trace, **result}


def decide_router(
    pool: ConnectionPool[Connection], config: str | None, bearer_token: Callable[[str | None], str]
) -> APIRouter:
    router = APIRouter()
    try:
        credentials = TypeAdapter(list[Credential]).validate_json(config or "[]")
    except ValidationError:
        raise ValueError("Invalid decision credentials") from None
    if len({entry.token for entry in credentials}) != len(credentials):
        raise ValueError("Decision credentials must be unique")

    @router.post("/decide")
    def endpoint(
        payload: DecideInput, authorization: Annotated[str | None, Header()] = None
    ) -> dict[str, Any]:
        token = bearer_token(authorization)
        tenant = next(
            (entry.tenant_id for entry in credentials if compare_digest(token, entry.token)), None
        )
        if tenant is None:
            raise HTTPException(403, "Decision credential is not authorized")
        try:
            with pool.connection() as conn, conn.transaction():
                return publish(conn, tenant, payload)
        except (Error, PoolTimeout, TooManyRequests):
            raise HTTPException(503, "Decision unavailable") from None

    return router
