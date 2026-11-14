from typing import Any
from uuid import UUID

from psycopg import Connection

from app.adapters.ingest import audit


def queue_reply(conn: Connection[Any], tenant: UUID, case: UUID, message: UUID) -> dict[str, Any]:
    draft = conn.execute(
        """
        SELECT d.id,d.status,d.policy_decision_id,p.decision,p.trace_id,
               p.revision=c.policy_revision AND p.draft_id=d.id AND p.draft_payload_hash=d.payload_hash
        FROM public.drafts d
        JOIN public.policy_decisions p ON (p.tenant_id,p.case_id,p.id)=(d.tenant_id,d.case_id,d.policy_decision_id)
        JOIN public.cases c ON (c.tenant_id,c.id)=(d.tenant_id,d.case_id)
        JOIN public.tenant_packs tp ON (tp.tenant_id,tp.id)=(c.tenant_id,c.tenant_pack_id)
        WHERE d.tenant_id=%s AND d.case_id=%s AND d.in_reply_to_message_id=%s
          AND d.status IN ('pending','approved','sent') AND tp.enabled
          AND coalesce(tp.config->'adapters'->>'send_reply','mock')='mock'
        FOR UPDATE OF c,d
        """,
        (tenant, case, message),
    ).fetchone()
    if draft is None or draft[3] not in ("auto", "needs_approval"):
        return {"queued": False}
    draft_id, status, policy_id, decision, trace, current = draft
    key = f"draft-send:{draft_id}"
    existing = conn.execute(
        "SELECT id FROM public.actions WHERE tenant_id=%s AND idempotency_key=%s",
        (tenant, key),
    ).fetchone()
    if existing:
        return {"queued": True, "action_id": existing[0]}
    if not current or (decision == "auto" and status != "approved") or status == "sent":
        return {"queued": False}
    latest = conn.execute(
        """SELECT id FROM public.messages WHERE tenant_id=%s AND case_id=%s AND direction='inbound'
           ORDER BY created_at DESC,id DESC LIMIT 1""",
        (tenant, case),
    ).fetchone()
    if latest != (message,):
        return {"queued": False}
    conn.execute(
        """UPDATE public.cases c SET queue_id=q.id FROM public.packs p, public.queues q
           WHERE c.tenant_id=%s AND c.id=%s AND c.queue_id IS NULL AND p.id=c.pack_id
             AND q.tenant_id=c.tenant_id AND q.key=p.definition->>'default_queue'""",
        (tenant, case),
    )
    action = conn.execute(
        """
        INSERT INTO public.actions (
            tenant_id,case_id,message_id,policy_decision_id,trace_id,idempotency_key,
            tool_key,risk_level,status,input,adapter
        ) VALUES (%s,%s,%s,%s,%s,%s,'send_reply','external_comm',%s,
                  jsonb_build_object('draft_id',%s::uuid),'mock')
        RETURNING id
        """,
        (
            tenant,
            case,
            message,
            policy_id,
            trace,
            key,
            "proposed",
            draft_id,
        ),
    ).fetchone()
    assert action is not None
    if decision == "needs_approval" and status == "pending":
        conn.execute(
            """INSERT INTO public.approval_requests (tenant_id,case_id,subject_type,subject_id,reason)
               VALUES (%s,%s,'draft',%s,'Review outbound reply')""",
            (tenant, case, draft_id),
        )
    audit(conn, tenant, trace, "action.queued", "action", action[0])
    return {"queued": True, "action_id": action[0]}
