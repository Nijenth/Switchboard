import re
from collections.abc import Sequence
from typing import Any

import pytest
from fastapi.testclient import TestClient

from harness.console import (
    SCENARIOS,
    SENDS_PER_MINUTE,
    SESSION_SEND_CAP,
    create_console,
    outcome_badges,
)
from harness.console_page import PAGE, SCRIPT
from harness.live_stack import MANAGER_SLACK, REVIEWER, StackError

REVIEW = "94000000-0000-0000-0000-000000000001"


class FakeBackend:
    """Every delivered external ID maps to one case, as core's idempotent ingest does."""

    def __init__(self) -> None:
        self.deliveries: list[tuple[str, str, str, str]] = []
        self.cases: dict[str, str] = {}
        self.advances = 0
        self.decisions: list[tuple[str, str, str]] = []
        self.role = "manager"
        self.waiting = True
        self.fail_ingest_before = self.fail_ingest_after = self.fail_advance = False

    def ingest(self, sender: str, text: str, external_id: str, date: str) -> None:
        if self.fail_ingest_before:
            raise StackError("n8n import:workflow exited 1")
        self.deliveries.append((sender, text, external_id, date))
        self.cases.setdefault(external_id, f"93000000-0000-0000-0000-{len(self.cases):012d}")
        if self.fail_ingest_after:
            raise StackError("n8n execute --id=SwitchboardEmail1 exited 1")

    def find_case(self, external_id: str) -> str | None:
        return self.cases.get(external_id)

    def advance(self) -> None:
        if self.fail_advance:
            raise StackError("n8n execute --id=SwitchboardSlack1 exited 1; HTTP statuses: ['503']")
        self.advances += 1

    def decide(self, review_id: str, reviewer: str, decision: str) -> None:
        self.decisions.append((review_id, reviewer, decision))
        self.waiting = False

    def timelines(self, case_ids: Sequence[str]) -> dict[str, list[dict[str, Any]]]:
        rows = [{"event_kind": "draft", "status": "pending_review", "draft_body": "Hello"}]
        return {case_id: rows for case_id in case_ids}

    def pending(self, case_ids: Sequence[str]) -> list[dict[str, Any]]:
        if not self.waiting or not case_ids or self.advances == 0:
            return []
        return [
            {"id": REVIEW, "case_id": case_ids[0], "required_role": self.role, "reason": "Review"}
        ]


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def api(backend: FakeBackend, clock: Clock) -> TestClient:
    app = create_console(backend, clock=clock)
    client = TestClient(app, base_url="http://127.0.0.1:8780")
    client.headers["X-Console-Token"] = app.state.console.token
    return client


def free(text: str, sender: str = "liam.chen@example.com") -> dict[str, str]:
    return {"sender": sender, "text": text}


def test_page_carries_the_token_and_the_mock_notice(api: TestClient) -> None:
    token = api.headers.pop("X-Console-Token")
    page = api.get("/")
    assert page.status_code == 200
    assert f'<meta name="console-token" content="{token}">' in page.text
    assert "mock stack" in page.text and "no real money moves" in page.text
    assert all(f'data-key="{scenario.key}"' in page.text for scenario in SCENARIOS)


def test_csp_allows_no_inline_script_or_style(api: TestClient) -> None:
    del api.headers["X-Console-Token"]
    page = api.get("/")
    policy = page.headers["Content-Security-Policy"]
    assert "unsafe-inline" not in policy and "unsafe-eval" not in policy
    assert "default-src 'none'" in policy
    assert "script-src 'self'" in policy and "style-src 'self'" in policy
    # The page itself must not need what the policy forbids.
    assert re.findall(r"<script\b[^>]*>", page.text) == ['<script src="/console.js">']
    assert "<style" not in page.text and " style=" not in page.text
    assert not re.search(r"\son[a-z]+\s*=", page.text)
    script, style = api.get("/console.js"), api.get("/console.css")
    assert script.status_code == 200 and script.headers["content-type"].startswith(
        "text/javascript"
    )
    assert style.status_code == 200 and style.headers["content-type"].startswith("text/css")
    # The static files carry no token and no session data.
    assert api.app.state.console.token not in script.text + style.text  # type: ignore[attr-defined]


def test_page_script_has_no_html_sink() -> None:
    for sink in (
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "eval(",
        "new Function",
        "setAttribute(",
        "srcdoc",
        "DOMParser",
        "createContextualFragment",
    ):
        assert sink not in SCRIPT, sink
    assert not re.search(r"set(Timeout|Interval)\(\s*[\"'`]", SCRIPT)
    assert "textContent" in SCRIPT
    assert "__TOKEN__" not in SCRIPT and PAGE.count("__TOKEN__") == 1


def test_markup_in_a_message_round_trips_as_data(api: TestClient, backend: FakeBackend) -> None:
    attack = "<script>alert(1)</script><img src=x onerror=alert(2)>"
    result = api.post("/api/send", json=free(attack))
    assert result.status_code == 200
    assert result.headers["content-type"].startswith("application/json")
    (case,) = result.json()["cases"]
    assert case["text"] == attack  # unchanged, as a JSON string
    assert backend.deliveries[0][1] == attack
    # The served page never contains user text: it is fetched as JSON and set with textContent.
    assert attack not in api.get("/").text
    assert 'card.append(el("blockquote", "", item.text));' in SCRIPT


@pytest.mark.parametrize("token", [None, "", "wrong"])
def test_api_requires_the_page_token(
    api: TestClient, backend: FakeBackend, token: str | None
) -> None:
    del api.headers["X-Console-Token"]
    headers = {} if token is None else {"X-Console-Token": token}
    assert api.get("/api/state", headers=headers).status_code == 403
    assert api.post("/api/send", json={"scenario": "verified"}, headers=headers).status_code == 403
    assert (
        api.post(
            f"/api/reviews/{REVIEW}", json={"decision": "approve"}, headers=headers
        ).status_code
        == 403
    )
    assert backend.deliveries == []


def test_foreign_host_name_is_refused(backend: FakeBackend) -> None:
    app = create_console(backend)
    client = TestClient(app, base_url="http://attacker.example")
    token = {"X-Console-Token": app.state.console.token}
    assert client.get("/", headers=token).status_code == 403
    assert client.get("/console.js", headers=token).status_code == 403
    assert client.post("/api/send", json={"scenario": "verified"}, headers=token).status_code == 403
    assert backend.deliveries == []


def test_scenario_sends_the_scripted_message(api: TestClient, backend: FakeBackend) -> None:
    result = api.post("/api/send", json={"scenario": "refund"})
    assert result.status_code == 200
    assert [delivery[:2] for delivery in backend.deliveries] == [
        ("sofia.rossi@example.com", "Please refund my delivered order NW-1003.")
    ]
    (case,) = result.json()["cases"]
    assert case["badges"] == ["Waiting for review"] and case["partial"] is False
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
    assert backend.deliveries == []


def test_free_text_is_sent_trimmed(api: TestClient, backend: FakeBackend) -> None:
    assert api.post("/api/send", json=free("  Do you sell gift cards?  ")).status_code == 200
    assert [delivery[:2] for delivery in backend.deliveries] == [
        ("liam.chen@example.com", "Do you sell gift cards?")
    ]


def test_failure_after_ingest_keeps_the_case_visible_as_partly_processed(
    api: TestClient, backend: FakeBackend
) -> None:
    backend.fail_advance = True
    failed = api.post("/api/send", json=free("Do you sell gift cards?"))
    assert failed.status_code == 502 and "A stack step failed" in failed.json()["detail"]
    (case,) = failed.json()["cases"]  # the failed response already carries the case
    assert case["partial"] is True and case["case_id"] is not None and case["problem"]
    assert api.get("/api/state").json()["cases"][0]["external_id"] == case["external_id"]

    # Retrying finishes the same case: nothing is delivered a second time.
    backend.fail_advance = False
    retried = api.post(f"/api/cases/{case['external_id']}/retry", json={})
    assert retried.status_code == 200
    (after,) = retried.json()["cases"]
    assert (after["case_id"], after["partial"]) == (case["case_id"], False)
    assert len(backend.deliveries) == 1 and len(backend.cases) == 1
    # A finished case has nothing left to retry.
    assert api.post(f"/api/cases/{case['external_id']}/retry", json={}).status_code == 404


def test_sending_the_same_input_again_retries_instead_of_creating_a_second_case(
    api: TestClient, backend: FakeBackend
) -> None:
    backend.fail_advance = True
    assert api.post("/api/send", json=free("Do you sell gift cards?")).status_code == 502
    backend.fail_advance = False
    again = api.post("/api/send", json=free("Do you sell gift cards?"))
    assert again.status_code == 200
    assert len(again.json()["cases"]) == 1 and len(backend.cases) == 1
    # Once it is finished, the same text is a new message.
    assert len(api.post("/api/send", json=free("Do you sell gift cards?")).json()["cases"]) == 2


def test_failure_inside_the_ingest_workflow_redelivers_the_same_message_on_retry(
    api: TestClient, backend: FakeBackend
) -> None:
    backend.fail_ingest_after = True  # core accepted the message, a later node failed
    failed = api.post("/api/send", json=free("Hello"))
    (case,) = failed.json()["cases"]
    assert failed.status_code == 502 and case["partial"] and case["case_id"] is not None
    backend.fail_ingest_after = False
    assert api.post(f"/api/cases/{case['external_id']}/retry", json={}).status_code == 200
    assert len(backend.cases) == 1 and len(backend.deliveries) == 1  # case known: no redelivery


def test_failure_before_ingest_is_retried_with_the_same_external_id_and_date(
    api: TestClient, backend: FakeBackend
) -> None:
    backend.fail_ingest_before = True
    failed = api.post("/api/send", json=free("Hello"))
    (case,) = failed.json()["cases"]
    assert failed.status_code == 502 and case["partial"] and case["case_id"] is None
    backend.fail_ingest_before = False
    assert api.post(f"/api/cases/{case['external_id']}/retry", json={}).status_code == 200
    assert api.post("/api/send", json=free("Hello")).status_code == 200  # a new, second message
    first, second = backend.deliveries
    assert first[2] == case["external_id"] and second[2] != first[2]


@pytest.mark.parametrize("external_id", ["switchboard-live-" + "0" * 32, "nope", "..%2F.."])
def test_retry_of_an_unknown_message_is_refused(
    api: TestClient, backend: FakeBackend, external_id: str
) -> None:
    assert api.post(f"/api/cases/{external_id}/retry", json={}).status_code == 404
    assert backend.deliveries == [] and backend.advances == 0


def test_sends_per_minute_are_limited_and_the_window_reopens(
    api: TestClient, backend: FakeBackend, clock: Clock
) -> None:
    for index in range(SENDS_PER_MINUTE):
        assert api.post("/api/send", json=free(f"message {index}")).status_code == 200
        clock.now += 1
    limited = api.post("/api/send", json=free("one too many"))
    assert limited.status_code == 429 and "Try again in" in limited.json()["detail"]
    assert int(limited.headers["Retry-After"]) > 0
    assert len(backend.deliveries) == SENDS_PER_MINUTE  # the refused send did no work
    clock.now += 60
    assert api.post("/api/send", json=free("one too many")).status_code == 200


def test_retries_count_toward_the_per_minute_limit(
    api: TestClient, backend: FakeBackend, clock: Clock
) -> None:
    backend.fail_advance = True
    failed = api.post("/api/send", json=free("Hello"))
    external_id = failed.json()["cases"][0]["external_id"]
    statuses = [
        api.post(f"/api/cases/{external_id}/retry", json={}).status_code
        for _ in range(SENDS_PER_MINUTE)
    ]
    assert statuses == [502] * (SENDS_PER_MINUTE - 1) + [429]


def test_total_sends_per_session_are_capped(backend: FakeBackend, clock: Clock) -> None:
    app = create_console(backend, clock=clock, session_send_cap=3, sends_per_minute=100)
    api = TestClient(app, base_url="http://127.0.0.1:8780")
    api.headers["X-Console-Token"] = app.state.console.token
    for index in range(3):
        assert api.post("/api/send", json=free(f"message {index}")).status_code == 200
    clock.now += 3600  # waiting does not lift the session cap
    capped = api.post("/api/send", json=free("message 3"))
    assert capped.status_code == 429 and "limit of 3 messages" in capped.json()["detail"]
    assert len(backend.deliveries) == 3
    assert SESSION_SEND_CAP >= 6 * 3  # room for every scenario several times


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
    assert backend.deliveries == []
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
