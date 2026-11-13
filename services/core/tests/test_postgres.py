from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from hashlib import sha256
from os import environ
from secrets import token_hex
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import Connection

from app.adapters.db import Settings, create_pool
from app.main import create_app

NORTHWIND = UUID("10000000-0000-0000-0000-000000000001")
ACME = UUID("91000000-0000-0000-0000-000000000001")
INVOICES = "invoices@northwind.example"
SUPPORT = "support@northwind.example"


@pytest.fixture(scope="module")
def settings() -> Settings:
    if not environ.get("SWITCHBOARD_CORE_TEST_MARKER"):
        pytest.skip("run scripts/test_core.ps1 to provision the disposable database")
    config = Settings.from_environment()
    assert config.db_host == "127.0.0.1" and config.db_name == "switchboard_test"
    return config


def verify_cluster(connection: Connection[tuple[object, ...]], login: str) -> None:
    result = connection.execute(
        "SELECT current_setting('switchboard.integration_marker', true), session_user, current_user"
    ).fetchone()
    assert result == (environ["SWITCHBOARD_CORE_TEST_MARKER"], login, login)


@pytest.fixture(scope="module")
def credentials(settings: Settings) -> dict[str, str]:
    addresses = [INVOICES, SUPPORT, "support@acme.example", "hello@northwind.example"]
    tokens = {address: token_hex(32) for address in addresses}
    with psycopg.connect(
        host=settings.db_host,
        port=settings.db_port,
        dbname=settings.db_name,
        user="switchboard_config_loader",
        password=environ["SWITCHBOARD_CONFIG_LOADER_PASSWORD"],
        connect_timeout=3,
    ) as connection:
        verify_cluster(connection, "switchboard_config_loader")
        for address, token in tokens.items():
            inserted = connection.execute(
                """
                INSERT INTO public.ingress_credentials (token_digest, tenant_id, endpoint_id, expires_at)
                SELECT %s, tenant_id, id, now() + interval '5 minutes'
                FROM public.channel_endpoints WHERE channel = 'email' AND address = %s
                """,
                (sha256(token.encode("ascii")).digest(), address),
            )
            assert inserted.rowcount == 1
    return tokens


@pytest.fixture
def client(settings: Settings, credentials: dict[str, str]) -> Iterator[TestClient]:
    # Verify identity with the same core connection settings before the app writes.
    with create_pool(settings, max_size=1) as pool, pool.connection() as connection:
        verify_cluster(connection, "switchboard_core")
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def make_payload(address: str) -> dict[str, object]:
    return {
        "trace_id": str(uuid4()),
        "idempotency_key": str(uuid4()),
        "channel": "email",
        "endpoint_address": address,
        "external_thread_id": str(uuid4()),
        "sender": {"address": "fixture@example.test"},
        "body_text": "Synthetic Stage 1 fixture",
        "form_fields": {"source": "fixture"},
        "received_at": datetime.now(UTC).isoformat(),
    }


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_health_uses_actual_core_login(client: TestClient, settings: Settings) -> None:
    assert client.get("/health", headers=bearer(settings.health_token)).status_code == 200


def test_ingest_scope_and_disabled_pack(client: TestClient, credentials: dict[str, str]) -> None:
    headers = bearer(credentials[INVOICES])
    payload = make_payload(INVOICES)
    assert client.post("/ingest", json=payload, headers=bearer(token_hex(32))).status_code == 401
    for field, value in (
        ("tenant_id", str(ACME)),
        ("endpoint_address", SUPPORT),
        ("channel", "api"),
    ):
        assert (
            client.post("/ingest", json={**payload, field: value}, headers=headers).status_code
            == 403
        )
    disabled = "support@acme.example"
    assert (
        client.post(
            "/ingest", json=make_payload(disabled), headers=bearer(credentials[disabled])
        ).status_code
        == 401
    )


def test_ingest_deduplicates_and_records_one_audit_trail(
    client: TestClient, credentials: dict[str, str], settings: Settings
) -> None:
    payload = make_payload(INVOICES)
    payload["external_thread_id"] = None

    def deliver() -> dict[str, object]:
        response = client.post("/ingest", json=payload, headers=bearer(credentials[INVOICES]))
        assert response.status_code == 200, response.text
        result: dict[str, object] = response.json()
        return result

    with ThreadPoolExecutor(max_workers=2) as workers:
        first, retry = sorted(
            workers.map(lambda _: deliver(), range(2)), key=lambda r: bool(r["duplicate"])
        )
    assert first["duplicate"] is False
    assert retry == {**first, "duplicate": True}
    assert first["tenant_id"] == str(NORTHWIND)
    assert first["trace_id"] == payload["trace_id"]

    with (
        create_pool(settings, max_size=1) as pool,
        pool.connection() as connection,
        connection.transaction(),
    ):
        verify_cluster(connection, "switchboard_core")
        connection.execute("SELECT set_config('app.tenant_id', %s, true)", (str(NORTHWIND),))
        message = connection.execute(
            """
            SELECT m.case_id, m.endpoint_id, m.body_text, m.form_fields, c.pack_id
            FROM public.messages m JOIN public.cases c ON c.tenant_id = m.tenant_id AND c.id = m.case_id
            WHERE m.tenant_id = %s AND m.id = %s
            """,
            (NORTHWIND, first["message_id"]),
        ).fetchone()
        assert message == (
            UUID(str(first["case_id"])),
            UUID(str(first["endpoint_id"])),
            payload["body_text"],
            payload["form_fields"],
            UUID("30000000-0000-0000-0000-000000000003"),
        )
        events = connection.execute(
            "SELECT event_type FROM public.audit_log WHERE tenant_id = %s AND trace_id = %s ORDER BY id",
            (NORTHWIND, first["trace_id"]),
        ).fetchall()
        assert events == [("case.created",), ("message.ingested",)]


def test_ingest_rejects_changed_content_for_an_existing_key(
    client: TestClient, credentials: dict[str, str]
) -> None:
    payload = make_payload(INVOICES)
    headers = bearer(credentials[INVOICES])
    first = client.post("/ingest", json=payload, headers=headers)
    assert first.status_code == 200
    retraced = {**payload, "trace_id": str(uuid4()), "tenant_id": str(NORTHWIND)}
    retry = client.post("/ingest", json=retraced, headers=headers)
    assert retry.status_code == 200
    assert retry.json() == {**first.json(), "duplicate": True}
    conflict = client.post("/ingest", json={**payload, "body_text": "Changed"}, headers=headers)
    assert conflict.status_code == 409
    assert client.post("/ingest", json=payload, headers=headers).json() == retry.json()


def test_ingest_attaches_threads_and_scopes_duplicates_by_endpoint(
    client: TestClient, credentials: dict[str, str]
) -> None:
    payload = make_payload(INVOICES)
    first = client.post("/ingest", json=payload, headers=bearer(credentials[INVOICES]))
    reply = client.post(
        "/ingest",
        json={**payload, "idempotency_key": str(uuid4())},
        headers=bearer(credentials[INVOICES]),
    )
    other = client.post(
        "/ingest",
        json={**payload, "endpoint_address": SUPPORT},
        headers=bearer(credentials[SUPPORT]),
    )
    assert first.status_code == reply.status_code == other.status_code == 200
    assert first.json()["case_id"] == reply.json()["case_id"]
    assert first.json()["message_id"] != reply.json()["message_id"]
    assert first.json()["case_id"] != other.json()["case_id"]


def test_core_pool_reuses_connection_after_commit_and_rollback(settings: Settings) -> None:
    with create_pool(settings, max_size=1) as pool:
        with pool.connection() as connection, connection.transaction():
            verify_cluster(connection, "switchboard_core")
            pid = connection.info.backend_pid
            connection.execute("SELECT set_config('app.tenant_id', %s, true)", (str(NORTHWIND),))
            assert connection.execute("SELECT DISTINCT tenant_id FROM public.cases").fetchall() == [
                (NORTHWIND,)
            ]

        class RollBack(Exception):
            pass

        with pytest.raises(RollBack), pool.connection() as connection, connection.transaction():
            assert connection.info.backend_pid == pid
            assert connection.execute("SELECT count(*) FROM public.cases").fetchone() == (0,)
            connection.execute("SELECT set_config('app.tenant_id', %s, true)", (str(ACME),))
            assert connection.execute("SELECT DISTINCT tenant_id FROM public.cases").fetchall() == [
                (ACME,)
            ]
            raise RollBack

        with pool.connection() as connection:
            assert connection.info.backend_pid == pid
            assert connection.execute("SELECT count(*) FROM public.cases").fetchone() == (0,)
