from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from hmac import compare_digest
from re import fullmatch
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from psycopg import DataError, Error, IntegrityError, OperationalError
from psycopg_pool import PoolTimeout, TooManyRequests
from pydantic import BaseModel

from app.adapters.db import Settings, create_pool
from app.adapters.ingest import IngressScopeMismatch, InvalidIngressCredential, ingest
from app.schemas.ingest import IngestInput, IngestResult


class Health(BaseModel):
    status: str
    database: str
    trace_id: UUID


def bearer_token(authorization: str | None) -> str:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token or not token.isascii():
        raise HTTPException(401, "Invalid credential", headers={"WWW-Authenticate": "Bearer"})
    return token


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings if settings is not None else Settings.from_environment()
    pool = create_pool(config)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        pool.open()
        try:
            yield
        finally:
            pool.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_: Request, _error: RequestValidationError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": "Invalid request"})

    @app.get("/health", response_model=Health)
    def health(
        authorization: Annotated[str | None, Header()] = None,
        x_trace_id: Annotated[UUID | None, Header()] = None,
    ) -> Health:
        if not compare_digest(bearer_token(authorization), config.health_token):
            raise HTTPException(401, "Invalid credential", headers={"WWW-Authenticate": "Bearer"})
        trace_id = x_trace_id or uuid4()
        try:
            with pool.connection() as connection:
                connection.execute("SELECT 1")
        except (Error, TooManyRequests):
            raise HTTPException(
                503, {"status": "unavailable", "database": "unavailable", "trace_id": str(trace_id)}
            ) from None
        return Health(status="ok", database="ok", trace_id=trace_id)

    @app.post("/ingest", response_model=IngestResult)
    def ingest_message(
        payload: IngestInput,
        authorization: Annotated[str | None, Header()] = None,
    ) -> IngestResult:
        token = bearer_token(authorization)
        if not fullmatch(r"[0-9a-f]{64}", token):
            raise HTTPException(401, "Invalid credential", headers={"WWW-Authenticate": "Bearer"})
        try:
            with pool.connection() as connection, connection.transaction():
                return ingest(connection, payload, token)
        except InvalidIngressCredential:
            raise HTTPException(
                401, "Invalid credential", headers={"WWW-Authenticate": "Bearer"}
            ) from None
        except IngressScopeMismatch:
            raise HTTPException(403, "Ingress scope mismatch") from None
        except DataError:
            raise HTTPException(422, "Invalid ingest data") from None
        except IntegrityError:
            raise HTTPException(409, "Ingest data conflicts with existing records") from None
        except (OperationalError, PoolTimeout, TooManyRequests):
            raise HTTPException(503, "Ingest unavailable") from None
        except Error:
            raise HTTPException(500, "Ingest failed") from None

    return app
