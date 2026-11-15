"""Contract tests: a stub of core's documented API and synthetic provider fixtures.

No network. Every provider and core call goes through httpx.MockTransport.
"""

import json
from base64 import urlsafe_b64decode
from datetime import UTC, datetime, timedelta
from email import message_from_bytes
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway import gmail, shopify
from gateway.main import Settings, create_app
from gateway.providers import Refused, UnknownOutcome

FIXTURES = Path(__file__).parent / "fixtures"
TENANT = "10000000-0000-0000-0000-000000000001"
OTHER_TENANT = "91000000-0000-0000-0000-000000000001"
ACTION = "95000000-0000-0000-0000-000000000001"
CALLER, WORKER, SHOP_TOKEN, MAIL_TOKEN = ("c" * 40, "w" * 40, "s" * 40, "m" * 40)
GRANT = "ab" * 32
SHOP = "https://northwind-dev.myshopify.com/admin/api/test-version/graphql.json"
MAILBOX = "support@northwind.example"
NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)


def fixture(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return loaded


def settings(**overrides: Any) -> Settings:
    config: dict[str, Any] = {
        "core_url": "http://core.internal:8000",
        "core_worker_tokens": [{"tenant_id": TENANT, "token": WORKER}],
        "callers": [{"tenant_id": TENANT, "token": CALLER}],
        "accounts": [
            {
                "tenant_id": TENANT,
                "tool_key": "order_refund_issue",
                "provider": "shopify",
                "destination": "northwind-dev.myshopify.com",
                "token": SHOP_TOKEN,
                "endpoint": SHOP,
            },
            {
                "tenant_id": TENANT,
                "tool_key": "send_reply",
                "provider": "gmail",
                "destination": MAILBOX,
                "token": MAIL_TOKEN,
            },
        ],
    }
    return Settings.model_validate({**config, **overrides})


def refund_dispatch(**overrides: Any) -> dict[str, Any]:
    return {
        "tool_key": "order_refund_issue",
        "adapter": "real",
        "destination": "northwind-dev.myshopify.com",
        "payload_hash": "00",
        "input": {"order_number": "NW-1003"},
        "amount": 210,
        "currency": "EUR",
        "idempotency_key": "key-1",
        "expires_at": (NOW + timedelta(seconds=45)).isoformat(),
        "trace_id": "96000000-0000-0000-0000-000000000001",
        **overrides,
    }


def reply_dispatch(**overrides: Any) -> dict[str, Any]:
    return {
        **refund_dispatch(tool_key="send_reply", destination=MAILBOX, amount=None, currency=None),
        "input": {"draft_id": "97000000-0000-0000-0000-000000000001"},
        "draft": {
            "id": "97000000-0000-0000-0000-000000000001",
            "channel": "email",
            "recipient": "emma.jones@example.com",
            "subject": None,
            "body": "Your order NW-1001 is shipped with UPS.",
        },
        **overrides,
    }


class Stub:
    """Records calls and replays queued responses for core and for a provider."""

    def __init__(self, dispatch: dict[str, Any], provider: list[httpx.Response | Exception]):
        self.dispatch = dispatch
        self.provider_responses = provider
        self.core_calls: list[tuple[str, Any, str]] = []
        self.provider_calls: list[httpx.Request] = []
        self.claim_status = self.consume_status = self.complete_status = 200

    def core(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.core_calls.append((request.url.path, body, request.headers["Authorization"]))
        if request.url.path.endswith("/claim"):
            return httpx.Response(self.claim_status, json={"action_id": ACTION, "grant": GRANT})
        if request.url.path.endswith("/consume"):
            return httpx.Response(self.consume_status, json=self.dispatch)
        return httpx.Response(self.complete_status, json={"status": "executed"})

    def provider(self, request: httpx.Request) -> httpx.Response:
        self.provider_calls.append(request)
        response = self.provider_responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def client(self, **overrides: Any) -> TestClient:
        app = create_app(
            settings(**overrides),
            core=httpx.Client(transport=httpx.MockTransport(self.core)),
            provider=httpx.Client(transport=httpx.MockTransport(self.provider)),
            now=lambda: NOW,
        )
        api = TestClient(app)
        api.headers["Authorization"] = f"Bearer {CALLER}"
        return api


def ok(name: str) -> httpx.Response:
    return httpx.Response(200, json=fixture(name))


def run(stub: Stub, **overrides: Any) -> httpx.Response:
    return stub.client(**overrides).post("/execute", json={"action_id": ACTION})


def test_refund_claims_consumes_as_real_refunds_once_and_completes() -> None:
    stub = Stub(refund_dispatch(), [ok("shopify_order.json"), ok("shopify_refund_created.json")])
    result = run(stub)
    assert result.status_code == 200, result.text
    assert result.json() == {
        "status": "executed",
        "external_ref": "gid://shopify/Refund/9002",
        "reused": False,
    }
    # Exactly the request bodies core's Consume and Complete models accept.
    assert stub.core_calls == [
        (f"/actions/{ACTION}/claim", None, f"Bearer {WORKER}"),
        (f"/actions/{ACTION}/consume", {"grant": GRANT, "adapter": "real"}, f"Bearer {WORKER}"),
        (
            f"/actions/{ACTION}/complete",
            {"grant": GRANT, "adapter": "real", "external_ref": "gid://shopify/Refund/9002"},
            f"Bearer {WORKER}",
        ),
    ]
    lookup, mutation = stub.provider_calls
    assert str(lookup.url) == SHOP and lookup.headers["X-Shopify-Access-Token"] == SHOP_TOKEN
    assert json.loads(lookup.content)["variables"] == {"query": 'name:"NW-1003"'}
    refund_input = json.loads(mutation.content)["variables"]["input"]
    assert refund_input == {
        "orderId": "gid://shopify/Order/5001",
        "note": "Switchboard refund switchboard:key-1",
        "notify": False,
        "transactions": [
            {
                "orderId": "gid://shopify/Order/5001",
                "parentId": "gid://shopify/OrderTransaction/7001",
                "amount": "210",
                "gateway": "bogus",
                "kind": "REFUND",
            }
        ],
    }


def test_refund_already_made_for_this_key_is_reused_not_repeated() -> None:
    stub = Stub(refund_dispatch(), [ok("shopify_order_already_refunded.json")])
    result = run(stub)
    assert result.status_code == 200
    assert result.json()["external_ref"] == "gid://shopify/Refund/9001"
    assert result.json()["reused"] is True
    assert len(stub.provider_calls) == 1  # the search only: no second refund


def test_reply_is_sent_once_with_a_stable_message_id() -> None:
    stub = Stub(reply_dispatch(), [ok("gmail_search_empty.json"), ok("gmail_sent.json")])
    result = run(stub)
    assert result.status_code == 200, result.text
    assert result.json()["external_ref"] == "18f0dddd0000eeee"
    search_call, send = stub.provider_calls
    expected_id = gmail.message_id(MAILBOX, "key-1")
    assert search_call.url.params["q"] == "rfc822msgid:" + expected_id.strip("<>")
    assert send.headers["Authorization"] == f"Bearer {MAIL_TOKEN}"
    message = message_from_bytes(urlsafe_b64decode(json.loads(send.content)["raw"]))
    assert (message["From"], message["To"]) == (MAILBOX, "emma.jones@example.com")
    assert message["Message-ID"] == expected_id and message["Subject"] == gmail.DEFAULT_SUBJECT
    assert message.get_payload().strip() == "Your order NW-1001 is shipped with UPS."


def test_reply_already_sent_for_this_key_is_reused() -> None:
    stub = Stub(reply_dispatch(), [ok("gmail_search_found.json")])
    result = run(stub)
    assert result.json() == {
        "status": "executed",
        "external_ref": "18f0aaaa0000bbbb",
        "reused": True,
    }
    assert len(stub.provider_calls) == 1


@pytest.mark.parametrize(
    ("dispatch", "detail"),
    [
        (refund_dispatch(adapter="mock"), "real adapter"),
        (refund_dispatch(destination="someone-else.myshopify.com"), "No credential"),
        (refund_dispatch(tool_key="invoice_schedule_payment"), "No credential"),
        (
            refund_dispatch(expires_at=(NOW + timedelta(seconds=5)).isoformat()),
            "Not enough of the lease",
        ),
    ],
)
def test_dispatch_the_gateway_must_not_act_on_never_reaches_a_provider(
    dispatch: dict[str, Any], detail: str
) -> None:
    stub = Stub(dispatch, [])
    result = run(stub)
    assert result.status_code == 409 and detail in result.json()["detail"]
    assert stub.provider_calls == []
    assert not any(path.endswith("/complete") for path, *_ in stub.core_calls)


@pytest.mark.parametrize("stage", ["claim", "consume"])
def test_core_refusal_stops_before_any_provider_call(stage: str) -> None:
    stub = Stub(refund_dispatch(), [])
    setattr(stub, f"{stage}_status", 409)
    assert run(stub).status_code == 409
    assert stub.provider_calls == []


@pytest.mark.parametrize("header", [None, "Bearer " + "x" * 40, f"Basic {CALLER}", WORKER])
def test_unknown_caller_is_refused_before_core_is_called(header: str | None) -> None:
    stub = Stub(refund_dispatch(), [])
    api = stub.client()
    del api.headers["Authorization"]
    headers = {} if header is None else {"Authorization": header}
    assert api.post("/execute", json={"action_id": ACTION}, headers=headers).status_code == 401
    assert stub.core_calls == [] and stub.provider_calls == []


def test_caller_for_a_tenant_without_a_core_credential_is_refused() -> None:
    stub = Stub(refund_dispatch(), [])
    result = run(stub, callers=[{"tenant_id": OTHER_TENANT, "token": CALLER}])
    assert result.status_code == 503
    assert stub.core_calls == [] and stub.provider_calls == []


@pytest.mark.parametrize(
    ("responses", "status"),
    [
        ([ok("shopify_no_order.json")], 422),
        ([ok("shopify_order.json"), ok("shopify_refund_user_errors.json")], 422),
        ([ok("shopify_order.json"), httpx.Response(401, json={})], 422),
        ([ok("shopify_order.json"), httpx.Response(503, json={})], 502),
        ([ok("shopify_order.json"), httpx.ReadTimeout("timeout")], 502),
        ([ok("shopify_order.json"), httpx.Response(200, content=b"not json")], 502),
    ],
)
def test_refused_and_unknown_outcomes_are_never_completed_or_retried(
    responses: list[httpx.Response | Exception], status: int
) -> None:
    stub = Stub(refund_dispatch(), responses)
    result = run(stub)
    assert result.status_code == status
    assert stub.provider_responses == []  # each response used once: the write is not retried
    assert not any(path.endswith("/complete") for path, *_ in stub.core_calls)


def test_lost_completion_is_reported_without_a_second_write() -> None:
    stub = Stub(refund_dispatch(), [ok("shopify_order.json"), ok("shopify_refund_created.json")])
    stub.complete_status = 503
    result = run(stub)
    assert result.status_code == 502 and "completion was not recorded" in result.json()["detail"]
    assert len(stub.provider_calls) == 2


def provider_client(responses: list[httpx.Response | Exception]) -> tuple[httpx.Client, Stub]:
    stub = Stub({}, responses)
    return httpx.Client(transport=httpx.MockTransport(stub.provider)), stub


def test_search_retries_then_succeeds_and_the_write_is_sent_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waits: list[float] = []
    monkeypatch.setattr("gateway.providers.sleep", waits.append)
    client, stub = provider_client(
        [httpx.Response(429), httpx.Response(502), ok("shopify_order.json")]
        + [ok("shopify_refund_created.json")]
    )
    effect = shopify.issue_refund(client, SHOP, SHOP_TOKEN, refund_dispatch(), 5)
    assert effect.external_ref == "gid://shopify/Refund/9002" and len(stub.provider_calls) == 4
    assert waits == [0.5, 1.0]  # exponential backoff between search attempts


def test_search_gives_up_after_three_attempts_without_writing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("gateway.providers.sleep", lambda _: None)
    client, stub = provider_client([httpx.Response(503)] * 3)
    with pytest.raises(Refused):
        shopify.issue_refund(client, SHOP, SHOP_TOKEN, refund_dispatch(), 5)
    assert len(stub.provider_calls) == 3


@pytest.mark.parametrize(
    "dispatch",
    [
        refund_dispatch(amount=0),
        refund_dispatch(amount="NaN"),
        refund_dispatch(amount=None),
        refund_dispatch(input={}),
        refund_dispatch(input={"order_number": 'NW-1003" OR name:"NW-1'}),
        refund_dispatch(currency=None),
        refund_dispatch(idempotency_key="has space"),
    ],
)
def test_malformed_refund_payload_is_refused_without_any_call(dispatch: dict[str, Any]) -> None:
    client, stub = provider_client([])
    with pytest.raises(Refused):
        shopify.issue_refund(client, SHOP, SHOP_TOKEN, dispatch, 5)
    assert stub.provider_calls == []


@pytest.mark.parametrize(
    ("amount", "currency"), [(210.01, "EUR"), (210, "USD")], ids=["too much", "wrong currency"]
)
def test_refund_that_does_not_fit_the_payment_is_refused_before_the_write(
    amount: float, currency: str
) -> None:
    client, stub = provider_client([ok("shopify_order.json")])
    with pytest.raises(Refused):
        shopify.issue_refund(
            client, SHOP, SHOP_TOKEN, refund_dispatch(amount=amount, currency=currency), 5
        )
    assert len(stub.provider_calls) == 1


@pytest.mark.parametrize(
    "draft",
    [
        {"recipient": "a@example.com\r\nBcc: evil@example.com"},
        {"recipient": "a@example.com, b@example.com"},
        {"subject": "Hello\r\nBcc: evil@example.com"},
        {"body": "   "},
        {"channel": "web_chat"},
    ],
)
def test_unsendable_draft_is_refused_without_any_call(draft: dict[str, str]) -> None:
    dispatch = reply_dispatch()
    dispatch["draft"] = {**dispatch["draft"], **draft}
    client, stub = provider_client([])
    with pytest.raises(Refused):
        gmail.send_reply(client, MAILBOX, MAIL_TOKEN, dispatch, 5)
    assert stub.provider_calls == []


def test_send_without_a_message_id_in_the_answer_is_an_unknown_outcome() -> None:
    client, _ = provider_client([ok("gmail_search_empty.json"), httpx.Response(200, json={})])
    with pytest.raises(UnknownOutcome):
        gmail.send_reply(client, MAILBOX, MAIL_TOKEN, reply_dispatch(), 5)


@pytest.mark.parametrize(
    "change",
    [
        {"callers": [{"tenant_id": TENANT, "token": WORKER}]},
        {"callers": [{"tenant_id": TENANT, "token": "short"}]},
        {"core_url": "not a url"},
        {
            "accounts": [
                {
                    "tenant_id": TENANT,
                    "tool_key": "send_reply",
                    "provider": "shopify",
                    "destination": "x",
                    "token": SHOP_TOKEN,
                    "endpoint": SHOP,
                }
            ]
        },
        {
            "accounts": [
                {
                    "tenant_id": TENANT,
                    "tool_key": "order_refund_issue",
                    "provider": "shopify",
                    "destination": "x",
                    "token": SHOP_TOKEN,
                    "endpoint": "http://northwind-dev.myshopify.com/graphql.json",
                }
            ]
        },
        {
            "accounts": [
                {
                    "tenant_id": TENANT,
                    "tool_key": "order_refund_issue",
                    "provider": "shopify",
                    "destination": "x",
                    "token": SHOP_TOKEN,
                }
            ]
        },
    ],
    ids=[
        "reused token",
        "short token",
        "bad core url",
        "wrong tool",
        "http endpoint",
        "no endpoint",
    ],
)
def test_bad_configuration_is_rejected_at_startup(change: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        settings(**change)


def test_missing_configuration_error_does_not_echo_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "SWITCHBOARD_GATEWAY_CONFIG", '{"core_url": "x", "secret": "' + CALLER + '"}'
    )
    with pytest.raises(ValueError) as error:
        Settings.from_environment()
    assert CALLER not in str(error.value)
