from collections.abc import Iterator
from contextlib import contextmanager
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from psycopg import OperationalError

from app.adapters.db import Settings
from app.main import create_app


@pytest.mark.parametrize("available", [True, False])
def test_health_checks_the_pool(monkeypatch: pytest.MonkeyPatch, available: bool) -> None:
    class Pool:
        def open(self) -> None:
            pass

        def close(self) -> None:
            pass

        @contextmanager
        def connection(self) -> Iterator["Pool"]:
            if not available:
                raise OperationalError("database detail must stay private")
            yield self

        def execute(self, query: str) -> None:
            assert query == "SELECT 1"

    monkeypatch.setattr("app.main.create_pool", lambda settings: Pool())
    settings = Settings("unused", 5432, "unused", "unused", "health-test-credential" * 2)
    trace_id = str(uuid4())
    with TestClient(create_app(settings)) as client:
        assert client.get("/health").status_code == 401
        response = client.get(
            "/health",
            headers={"Authorization": f"Bearer {settings.health_token}", "X-Trace-Id": trace_id},
        )
    assert response.status_code == (200 if available else 503)
    status = "ok" if available else "unavailable"
    expected = {"status": status, "database": status, "trace_id": trace_id}
    assert response.json() == (expected if available else {"detail": expected})
