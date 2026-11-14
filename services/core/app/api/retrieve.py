from collections.abc import Callable
from hmac import compare_digest
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException
from psycopg import Connection, Error
from psycopg_pool import ConnectionPool, PoolTimeout, TooManyRequests
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from app.ai_accounting import set_scope
from app.api.understand import Credential


class RetrieveInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: UUID
    message_id: UUID
    limit: Annotated[int, Field(ge=1, le=5, strict=True)] = 3


class KnowledgeHit(BaseModel):
    chunk_id: UUID
    document_id: UUID
    title: str
    content: str
    score: float


class RetrieveResult(BaseModel):
    trace_id: UUID
    hits: list[KnowledgeHit]


def search_knowledge(
    conn: Connection, redacted: str | None, tenant: UUID, pack_key: str, version: int, limit: int
) -> list[KnowledgeHit]:
    hits = conn.execute(
        """
        WITH query AS MATERIALIZED (SELECT public.offline_knowledge_embedding(%s) AS vec),
        scoped AS MATERIALIZED (
          SELECT c.id,d.id AS document_id,d.title,c.content,
                 1 - (c.embedding OPERATOR(public.<=>) query.vec) AS score
          FROM public.knowledge_chunks c JOIN public.knowledge_documents d
            ON (d.tenant_id,d.id,d.pack_key)=(c.tenant_id,c.document_id,c.pack_key)
          CROSS JOIN query
          WHERE c.tenant_id=%s AND c.pack_key=%s AND d.version=%s AND d.status='active'
            AND c.embedding_model='offline-token-hash-v1' AND c.embedding IS NOT NULL
            AND query.vec IS NOT NULL
        )
        SELECT id,document_id,title,content,score FROM scoped
        WHERE score>0 ORDER BY score DESC,id LIMIT %s
        """,
        (redacted, tenant, pack_key, version, limit),
    ).fetchall()
    return [
        KnowledgeHit(chunk_id=chunk, document_id=doc, title=title, content=content, score=score)
        for chunk, doc, title, content, score in hits
    ]


def retrieve_router(
    pool: ConnectionPool[Connection], config: str | None, bearer_token: Callable[[str | None], str]
) -> APIRouter:
    router = APIRouter()
    try:
        credentials = TypeAdapter(list[Credential]).validate_json(config or "[]")
    except ValidationError:
        raise ValueError("Invalid retrieval credentials") from None
    if len({entry.token for entry in credentials}) != len(credentials):
        raise ValueError("Retrieval credentials must be unique")

    @router.post("/retrieve")
    def retrieve(
        request: RetrieveInput, authorization: Annotated[str | None, Header()] = None
    ) -> RetrieveResult:
        token = bearer_token(authorization)
        tenant = next(
            (entry.tenant_id for entry in credentials if compare_digest(token, entry.token)), None
        )
        if tenant is None:
            raise HTTPException(403, "Retrieval credential is not authorized")
        try:
            with pool.connection() as conn, conn.transaction():
                set_scope(conn, tenant)
                message = conn.execute(
                    """
                    SELECT m.trace_id,m.body_redacted,p.key,p.version
                    FROM public.messages m
                    JOIN public.cases c ON (c.tenant_id,c.id)=(m.tenant_id,m.case_id)
                    JOIN public.tenant_packs tp ON (tp.tenant_id,tp.id)=(c.tenant_id,c.tenant_pack_id)
                    JOIN public.packs p ON p.id=c.pack_id AND p.key=tp.pack_key
                    WHERE m.tenant_id=%s AND c.id=%s AND m.id=%s
                      AND m.direction='inbound' AND tp.enabled
                    FOR SHARE OF c
                    """,
                    (tenant, request.case_id, request.message_id),
                ).fetchone()
                if message is None:
                    raise HTTPException(404, "Retrieval message unavailable")
                trace_id, redacted, pack_key, version = message
                hits = search_knowledge(conn, redacted, tenant, pack_key, version, request.limit)
        except (Error, PoolTimeout, TooManyRequests):
            raise HTTPException(503, "Knowledge retrieval unavailable") from None
        return RetrieveResult(trace_id=trace_id, hits=hits)

    return router
