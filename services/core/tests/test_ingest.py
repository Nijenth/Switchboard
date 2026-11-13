from datetime import UTC, datetime
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.adapters.db import Settings
from app.main import create_app


@pytest.mark.parametrize("failure", ["missing_token", "attachment", "malformed"])
def test_ingest_rejects_unsupported_requests_before_database_access(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    pool = Mock()
    monkeypatch.setattr("app.main.create_pool", lambda settings: pool)
    payload: dict[str, object] = {
        "trace_id": str(uuid4()),
        "idempotency_key": "email:fixture",
        "channel": "email",
        "endpoint_address": "support@example.test",
        "sender": {"address": "sender@example.test"},
        "body_text": "Fixture text",
        "received_at": datetime.now(UTC).isoformat(),
    }
    if failure == "attachment":
        payload["attachments"] = [
            {
                "filename": "file.pdf",
                "mime_type": "application/pdf",
                "size_bytes": 10,
                "storage_key": "fixture",
            }
        ]
    if failure == "malformed":
        payload["channel"] = "invalid"
    headers = {} if failure == "missing_token" else {"Authorization": "Bearer " + "a" * 64}
    settings = Settings("unused", 5432, "unused", "unused", "health-test-credential" * 2)
    with TestClient(create_app(settings)) as client:
        response = client.post("/ingest", json=payload, headers=headers)
    assert response.status_code == (401 if failure == "missing_token" else 422)
    assert "Fixture text" not in response.text
    pool.connection.assert_not_called()
