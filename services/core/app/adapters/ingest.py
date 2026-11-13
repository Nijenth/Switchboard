from datetime import UTC
from hashlib import sha256
from json import dumps
from typing import Any
from uuid import UUID

from psycopg import Connection
from psycopg.types.json import Jsonb

from app.schemas.ingest import IngestInput, IngestResult


class InvalidIngressCredential(Exception):
    pass


class IngressScopeMismatch(Exception):
    pass


class IngestIdempotencyConflict(Exception):
    pass


def delivery_hash(payload: IngestInput) -> bytes:
    content = payload.model_dump(mode="json", exclude={"tenant_id", "trace_id", "idempotency_key"})
    content["received_at"] = payload.received_at.astimezone(UTC).isoformat()
    canonical = dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return sha256(canonical.encode("ascii")).digest()


def ingest(
    connection: Connection[tuple[Any, ...]], payload: IngestInput, token: str
) -> IngestResult:
    endpoint = connection.execute(
        "SELECT * FROM public.resolve_channel_endpoint(%s)",
        (sha256(token.encode("ascii")).digest(),),
    ).fetchone()
    if endpoint is None:
        raise InvalidIngressCredential
    tenant_id, tenant_pack_id, endpoint_id, pack_id, channel, address = endpoint
    if (
        (payload.tenant_id is not None and payload.tenant_id != tenant_id)
        or payload.channel != channel
        or payload.endpoint_address != address
    ):
        raise IngressScopeMismatch

    request_hash = delivery_hash(payload)
    connection.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant_id),))
    # Serialize retries before creating a case, including deliveries without a thread ID.
    connection.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"switchboard.ingest:{endpoint_id}:{payload.idempotency_key}",),
    )
    existing = connection.execute(
        """
        SELECT m.case_id, m.id, m.trace_id, tp.enabled, m.request_hash
        FROM public.messages m
        JOIN public.cases c ON c.tenant_id = m.tenant_id AND c.id = m.case_id
        LEFT JOIN public.tenant_packs tp ON tp.tenant_id = c.tenant_id AND tp.id = c.tenant_pack_id
        WHERE m.tenant_id = %s AND m.endpoint_id = %s AND m.idempotency_key = %s
          AND m.direction = 'inbound'
        """,
        (tenant_id, endpoint_id, payload.idempotency_key),
    ).fetchone()
    if existing is not None:
        case_id, message_id, trace_id, enabled, previous_hash = existing
        if enabled is False:
            raise IngressScopeMismatch
        if previous_hash != request_hash:
            raise IngestIdempotencyConflict
        return IngestResult(
            trace_id=trace_id,
            tenant_id=tenant_id,
            endpoint_id=endpoint_id,
            case_id=case_id,
            message_id=message_id,
            duplicate=True,
        )

    case_id = get_case(connection, payload, tenant_id, tenant_pack_id, pack_id, endpoint_id)
    message = connection.execute(
        """
        INSERT INTO public.messages (
            tenant_id, case_id, endpoint_id, trace_id, idempotency_key, request_hash, direction,
            channel, external_message_id, sender_address, subject, body_text, form_fields, received_at
        ) VALUES (%s, %s, %s, %s, %s, %s, 'inbound', %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            tenant_id,
            case_id,
            endpoint_id,
            payload.trace_id,
            payload.idempotency_key,
            request_hash,
            payload.channel,
            payload.external_message_id,
            payload.sender.address,
            payload.subject,
            payload.body_text,
            Jsonb(payload.form_fields),
            payload.received_at,
        ),
    ).fetchone()
    assert message is not None
    message_id = message[0]
    audit(connection, tenant_id, payload.trace_id, "message.ingested", "message", message_id)
    return IngestResult(
        trace_id=payload.trace_id,
        tenant_id=tenant_id,
        endpoint_id=endpoint_id,
        case_id=case_id,
        message_id=message_id,
        duplicate=False,
    )


def get_case(
    connection: Connection[tuple[Any, ...]],
    payload: IngestInput,
    tenant_id: UUID,
    tenant_pack_id: UUID | None,
    pack_id: UUID | None,
    endpoint_id: UUID,
) -> UUID:
    # A concurrent delivery can create the thread first. Read that row in a new statement.
    created = connection.execute(
        """
        INSERT INTO public.cases (
            tenant_id, tenant_pack_id, pack_id, channel, endpoint_id, external_thread_id, subject
        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (tenant_id, channel, endpoint_id, external_thread_id)
            WHERE external_thread_id IS NOT NULL DO NOTHING
        RETURNING id
        """,
        (
            tenant_id,
            tenant_pack_id,
            pack_id,
            payload.channel,
            endpoint_id,
            payload.external_thread_id,
            payload.subject,
        ),
    ).fetchone()
    if created is not None:
        case_id: UUID = created[0]
        audit(connection, tenant_id, payload.trace_id, "case.created", "case", case_id)
        return case_id

    existing = connection.execute(
        """
        SELECT c.id FROM public.cases c
        LEFT JOIN public.tenant_packs tp ON tp.tenant_id = c.tenant_id AND tp.id = c.tenant_pack_id
        WHERE c.tenant_id = %s AND c.channel = %s AND c.endpoint_id = %s
          AND c.external_thread_id = %s AND (c.tenant_pack_id IS NULL OR tp.enabled)
        """,
        (tenant_id, payload.channel, endpoint_id, payload.external_thread_id),
    ).fetchone()
    if existing is None:
        raise IngressScopeMismatch
    existing_id: UUID = existing[0]
    connection.execute(
        "UPDATE public.cases SET last_activity_at = now() WHERE tenant_id = %s AND id = %s",
        (tenant_id, existing_id),
    )
    return existing_id


def audit(
    connection: Connection[tuple[Any, ...]],
    tenant_id: UUID,
    trace_id: UUID,
    event_type: str,
    entity_type: str,
    entity_id: UUID,
) -> None:
    connection.execute(
        """
        INSERT INTO public.audit_log (tenant_id, trace_id, actor_type, event_type, entity_type, entity_id)
        VALUES (%s, %s, 'system', %s, %s, %s)
        """,
        (tenant_id, trace_id, event_type, entity_type, entity_id),
    )
