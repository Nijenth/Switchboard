from collections.abc import Callable
from hmac import compare_digest
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException
from psycopg import Connection, Error, OperationalError
from psycopg_pool import ConnectionPool, PoolTimeout, TooManyRequests
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Checkpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chain_seq: Annotated[int, Field(ge=0, le=2**63 - 1, strict=True)]
    row_hash: Annotated[str, Field(pattern=r"^(GENESIS|[0-9a-f]{64})$")]

    @model_validator(mode="after")
    def check_genesis(self) -> "Checkpoint":
        if (self.chain_seq == 0) != (self.row_hash == "GENESIS"):
            raise ValueError("Invalid audit checkpoint")
        return self


def audit_router(
    pool: ConnectionPool[Connection], token: str | None, bearer_token: Callable[[str | None], str]
) -> APIRouter:
    router = APIRouter(prefix="/audit")

    @router.post("/verify", response_model=Checkpoint)
    def verify(
        checkpoint: Checkpoint,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Checkpoint:
        if not token or not compare_digest(bearer_token(authorization), token):
            raise HTTPException(403, "Audit credential is not authorized")
        try:
            with pool.connection() as connection:
                row = connection.execute(
                    "SELECT chain_seq, row_hash, broken_at FROM public.verify_audit_checkpoint(%s,%s)",
                    (checkpoint.chain_seq, checkpoint.row_hash),
                ).fetchone()
        except (OperationalError, PoolTimeout, TooManyRequests):
            raise HTTPException(503, "Audit verification unavailable") from None
        except Error:
            raise HTTPException(500, "Audit verification failed") from None
        assert row is not None
        seq, row_hash, broken_at = row
        if broken_at is not None:
            raise HTTPException(409, {"message": "Audit chain mismatch", "broken_at": broken_at})
        return Checkpoint(chain_seq=seq, row_hash=row_hash)

    return router
