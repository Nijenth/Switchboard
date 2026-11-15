from collections.abc import Sequence
from typing import Any

import pytest
from fastapi.testclient import TestClient

from harness.console import SCENARIOS, create_console, outcome_badges
from harness.live_stack import MANAGER_SLACK, REVIEWER, StackError

CASE = "93000000-0000-0000-0000-000000000001"
REVIEW = "94000000-0000-0000-0000-000000000001"


class FakeBackend:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.decisions: list[tuple[str, str, str]] = []
        self.role = "manager"
        self.waiting = True
        self.fail = False

    def send(self, sender: str, text: str) -> str:
        if self.fail:
            raise StackError("n8n execute exited 1; HTTP statuses: ['503']")
        self.sent.append((sender, text))
        return CASE

    def decide(self, review_id: str, reviewer: str, decision: str) -> None:
        self.decisions.append((review_id, reviewer, decision))
        self.waiting = False

    def timelines(self, case_ids: Sequence[str]) -> dict[str, list[dict[str, Any]]]:
        rows = [{"event_kind": "draft", "status": "pending_review", "draft_body": "Hello"}]
        return {case_id: rows for case_id in case_ids}

    def pending(self, case_ids: Sequence[str]) -> list[dict[str, Any]]:
        if not self.waiting or CASE not in case_ids:
            return []
        return [{"id": REVIEW, "case_id": CASE, "required_role": self.role, "reason": "Review"}]


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def api(backend: FakeBackend) -> TestClient:
    app = create_console(backend)
    client = TestClient(app, base_url="http://127.0.0.1:8780")
    client.headers["X-Console-Token"] = app.state.console.token
    return client


def test_page_carries_the_token_and_the_mock_notice(api: TestClient) -> None:
    token = api.headers.pop("X-Console-Token")
    page = api.get("/")
    assert page.status_code == 200
    assert token in page.text
    assert "mock stack" in page.text and "no real money moves" in page.text
    assert "default-src 'none'" in page.headers["Content-Security-Policy"]
    assert all(f'data-key="{scenario.key}"' in page.text for scenario in SCENARIOS)


@pytest.mark.parametrize("token", [None, "", "wrong"])
def test_api_requires_the_page_token(
    api: TestClient, backend: FakeBackend, token: str | None
) -> None:
    del api.headers["X-Console-Token"]
    headers = {} if token is None else {"X-Console-Token": token}
    assert api.get("/api/state", headers=headers).status_code == 403
    assert api.post("/api/send", json={"scenario": "verified"}, headers=headers).status_code == 403
    assert backend.sent == []


def test_foreign_host_name_is_refused(backend: FakeBackend) -> None:
    app = create_console(backend)
    client = TestClient(app, base_url="http://attacker.example")
    token = {"X-Console-Token": app.state.console.token}
    assert client.get("/", headers=token).status_code == 403
    assert client.post("/api/send", json={"scenario": "verified"}, headers=token).status_code == 403
    assert backend.sent == []


def test_scenario_sends_the_scripted_message(api: TestClient, backend: FakeBackend) -> None:
    result = api.post("/api/send", json={"scenario": "refund"})
    assert result.status_code == 200
    assert backend.sent == [
        ("sofia.rossi@example.com", "Please refund my delivered order NW-1003.")
    ]
    (case,) = result.json()["cases"]
    assert case["badges"] == ["Waiting for review"]
    assert (case["reply"], case["reply_status"]) == ("Hello", "pending_review")


@pytest.mark.parametrize(
    "body",
    [
        {"scenario": "nope"},
        {"scenario": "refund", "text": "extra"},
        {"sender": "not-an-address", "text": "Hello"},
        {"sender": "a@example.com", "text": "   "},
        {"sender": "a@example.com", "text": "x" * 2001},
        {"sender": "a@example.com"},
        {},
    ],
)
def test_invalid_send_is_rejected(
    api: TestClient, backend: FakeBackend, body: dict[str, str]
) -> None:
    assert api.post("/api/send", json=body).status_code == 422
    assert backend.sent == []


def test_free_text_is_sent_trimmed(api: TestClient, backend: FakeBackend) -> None:
    body = {"sender": "liam.chen@example.com", "text": "  Do you sell gift cards?  "}
    assert api.post("/api/send", json=body).status_code == 200
    assert backend.sent == [("liam.chen@example.com", "Do you sell gift cards?")]


@pytest.mark.parametrize(("role", "reviewer"), [("manager", MANAGER_SLACK), ("agent", REVIEWER)])
def test_decision_is_signed_by_the_fixture_reviewer_for_the_role(
    api: TestClient, backend: FakeBackend, role: str, reviewer: str
) -> None:
    backend.role = role
    api.post("/api/send", json={"scenario": "refund"})
    result = api.post(f"/api/reviews/{REVIEW}", json={"decision": "approve"})
    assert result.status_code == 200 and result.json()["reviews"] == []
    assert backend.decisions == [(REVIEW, reviewer, "approve")]


def test_only_a_waiting_review_from_this_session_can_be_decided(
    api: TestClient, backend: FakeBackend
) -> None:
    # Nothing was sent in this console session, so no review belongs to it.
    assert api.post(f"/api/reviews/{REVIEW}", json={"decision": "approve"}).status_code == 404
    api.post("/api/send", json={"scenario": "refund"})
    assert api.post(f"/api/reviews/{REVIEW}", json={"decision": "edit"}).status_code == 422
    assert api.post("/api/reviews/not-a-uuid", json={"decision": "approve"}).status_code == 422
    assert backend.decisions == []


def test_second_step_is_refused_while_one_runs(api: TestClient, backend: FakeBackend) -> None:
    console = api.app.state.console  # type: ignore[attr-defined]
    with console._exclusive():
        assert api.post("/api/send", json={"scenario": "verified"}).status_code == 409
    assert backend.sent == []
    assert api.post("/api/send", json={"scenario": "verified"}).status_code == 200


def test_stack_failure_is_reported_and_releases_the_lock(
    api: TestClient, backend: FakeBackend
) -> None:
    backend.fail = True
    failed = api.post("/api/send", json={"scenario": "verified"})
    assert failed.status_code == 502 and "A stack step failed" in failed.json()["detail"]
    backend.fail = False
    assert api.post("/api/send", json={"scenario": "verified"}).status_code == 200


def test_badges_come_only_from_recorded_evidence() -> None:
    assert outcome_badges([], waiting=False) == []
    assert outcome_badges(
        [
            {"event_kind": "policy_decision", "decision": "auto", "rule_id": "ORDER_STATUS"},
            {"event_kind": "draft", "status": "sent"},
        ],
        waiting=False,
    ) == ["Reply sent automatically"]
    assert outcome_badges(
        [
            {"event_kind": "approval", "decision": "approved"},
            {"event_kind": "action", "risk_level": "money", "status": "executed"},
        ],
        waiting=False,
    ) == ["Mock refund paid after approval"]
    # A pending money action is not a payout, and a proposed draft is not a sent reply.
    assert outcome_badges(
        [
            {"event_kind": "action", "risk_level": "money", "status": "pending_approval"},
            {"event_kind": "draft", "status": "pending_review"},
        ],
        waiting=True,
    ) == ["Waiting for review"]
    assert outcome_badges(
        [
            {"event_kind": "policy_decision", "decision": "escalate", "rule_id": "G1"},
            {
                "event_kind": "lookup",
                "tool_key": "order_identity_check",
                "result_reason": "identity_unverified",
            },
            {
                "event_kind": "lookup",
                "tool_key": "order_refund_eligibility",
                "result_reason": "window_expired",
            },
            {"event_kind": "approval", "decision": "rejected"},
        ],
        waiting=False,
    ) == [
        "Escalated to a person",
        "Identity not verified: order data withheld",
        "Refund blocked: outside the refund window",
        "Rejected by reviewer",
    ]
    assert outcome_badges(
        [
            {"event_kind": "approval", "decision": "rejected"},
            {"event_kind": "draft", "status": "rejected"},
        ],
        waiting=False,
    ) == ["Reply rejected by reviewer"]
