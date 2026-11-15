"""Contract tests: a stub of core's documented API and synthetic provider fixtures.

No network. Every provider and core call goes through httpx.MockTransport.
"""

import asyncio
import json
from base64 import urlsafe_b64decode
from collections.abc import Callable
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
            "subject": "Re: Where is my order?",
            "thread_subject": "Where is my order?",
            "thread_id": "18f0abc00000abcd",
            "in_reply_to": "<order-question@example.com>",
            "references": "<earlier@example.com> <order-question@example.com>",
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

    def client(self, *, clock: Callable[[], float] = lambda: 0, **overrides: Any) -> TestClient:
        app = create_app(
            settings(**overrides),
            core=httpx.AsyncClient(transport=httpx.MockTransport(self.core)),
            provider=httpx.AsyncClient(transport=httpx.MockTransport(self.provider)),
            now=lambda: NOW,
            clock=clock,
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
    payload = json.loads(mutation.content)
    assert "@idempotent(key: $key)" in payload["query"]
    assert payload["variables"]["key"] == "key-1"
    refund_input = payload["variables"]["input"]
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
    payload = json.loads(send.content)
    assert payload["threadId"] == "18f0abc00000abcd"
    assert search_call.url.params["labelIds"] == "SENT"
    message = message_from_bytes(urlsafe_b64decode(payload["raw"]))
    assert message["In-Reply-To"] == "<order-question@example.com>"
    assert message["References"] == "<earlier@example.com> <order-question@example.com>"
    assert (message["From"], message["To"]) == (MAILBOX, "emma.jones@example.com")
    assert message["Message-ID"] == expected_id and message["Subject"] == "Re: Where is my order?"
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


def test_redirect_with_empty_body_does_not_authorize_gmail_send() -> None:
    stub = Stub(reply_dispatch(), [httpx.Response(302, json={})])
    assert run(stub).status_code == 422
    assert len(stub.provider_calls) == 1
    assert not any(path.endswith("/complete") for path, *_ in stub.core_calls)


def test_shopify_destination_must_match_endpoint_host() -> None:
    accounts = settings().model_dump(mode="json")["accounts"]
    accounts[0]["endpoint"] = "https://another.myshopify.com/admin/api/2026-04/graphql.json"
    with pytest.raises(ValueError, match="host must match the destination"):
        settings(accounts=accounts)


@pytest.mark.parametrize("statuses", [["PENDING"], ["FAILURE"], ["SUCCESS", "FAILURE"], []])
@pytest.mark.parametrize("reused", [False, True])
def test_only_settled_refunds_are_completed(statuses: list[str], reused: bool) -> None:
    if reused:
        body = fixture("shopify_order_already_refunded.json")
        refund = body["data"]["orders"]["nodes"][0]["refunds"][0]
        responses = []
    else:
        body = fixture("shopify_refund_created.json")
        refund = body["data"]["refundCreate"]["refund"]
        responses = [ok("shopify_order.json")]
    refund["transactions"]["nodes"] = [{"status": status} for status in statuses]
    responses.append(httpx.Response(200, json=body))
    stub = Stub(refund_dispatch(), responses)
    result = run(stub)
    if statuses == ["FAILURE"]:
        assert result.status_code == 422
        assert result.json()["detail"]["status"] == "failed"
    else:
        assert result.status_code == 502
        assert "outcome unknown" in result.json()["detail"]
    assert len(stub.provider_calls) == (1 if reused else 2)
    assert not any(path.endswith("/complete") for path, *_ in stub.core_calls)


IN_PROGRESS = {
    "field": None,
    "message": "This request is currently in progress, please try again.",
}
KEY_MISMATCH = {
    "field": None,
    "message": "The same idempotency key cannot be used with different operation parameters.",
}
NAMES_FIELD = {"field": ["transactions", "0", "amount"], "message": "Amount is too high"}


@pytest.mark.parametrize(
    "errors",
    [
        [NAMES_FIELD],
        [IN_PROGRESS],
        [KEY_MISMATCH],
        [{"field": None, "message": "Something went wrong"}],
        [{"field": [], "message": "Something went wrong"}],
        [{"message": "Something went wrong"}],
        ["not an object"],
        [NAMES_FIELD, {"field": None, "message": "This request is in progress."}],
    ],
)
def test_refund_user_errors_are_unknown_until_recorded(errors: list[Any]) -> None:
    body = {"data": {"refundCreate": {"refund": None, "userErrors": errors}}}
    stub = Stub(refund_dispatch(), [ok("shopify_order.json"), httpx.Response(200, json=body)])
    result = run(stub)
    assert result.status_code == 502, result.text
    assert "outcome unknown" in result.json()["detail"]
    assert len(stub.provider_calls) == 2  # the write is never repeated
    assert not any(path.endswith("/complete") for path, *_ in stub.core_calls)


def test_recorded_refund_user_error_is_refused_only_when_exact_and_refund_null(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert shopify.RECORDED_REFUSALS == frozenset()
    recorded = frozenset({(("transactions", "0", "amount"), "Amount is too high")})
    monkeypatch.setattr(shopify, "RECORDED_REFUSALS", recorded)

    refused_body = {"data": {"refundCreate": {"refund": None, "userErrors": [NAMES_FIELD]}}}
    stub = Stub(
        refund_dispatch(), [ok("shopify_order.json"), httpx.Response(200, json=refused_body)]
    )
    assert run(stub).status_code == 422
    assert len(stub.provider_calls) == 2
    assert not any(path.endswith("/complete") for path, *_ in stub.core_calls)

    other_message = {
        "data": {
            "refundCreate": {
                "refund": None,
                "userErrors": [{"field": ["transactions", "0", "amount"], "message": "Other"}],
            }
        }
    }
    stub_other = Stub(
        refund_dispatch(), [ok("shopify_order.json"), httpx.Response(200, json=other_message)]
    )
    assert run(stub_other).status_code == 502
    assert not any(path.endswith("/complete") for path, *_ in stub_other.core_calls)

    mixed_body = {
        "data": {
            "refundCreate": {
                "refund": None,
                "userErrors": [NAMES_FIELD, {"field": ["note"], "message": "Other error"}],
            }
        }
    }
    stub_mixed = Stub(
        refund_dispatch(), [ok("shopify_order.json"), httpx.Response(200, json=mixed_body)]
    )
    assert run(stub_mixed).status_code == 502
    assert not any(path.endswith("/complete") for path, *_ in stub_mixed.core_calls)

    with_refund = fixture("shopify_refund_created.json")
    with_refund["data"]["refundCreate"]["userErrors"] = [NAMES_FIELD]
    stub_refund = Stub(
        refund_dispatch(), [ok("shopify_order.json"), httpx.Response(200, json=with_refund)]
    )
    assert run(stub_refund).status_code == 502
    assert not any(path.endswith("/complete") for path, *_ in stub_refund.core_calls)


def test_refund_record_with_user_errors_is_unknown() -> None:
    body = fixture("shopify_refund_created.json")
    body["data"]["refundCreate"]["userErrors"] = [{"message": "Synthetic error"}]
    stub = Stub(refund_dispatch(), [ok("shopify_order.json"), httpx.Response(200, json=body)])
    assert run(stub).status_code == 502
    assert not any(path.endswith("/complete") for path, *_ in stub.core_calls)


@pytest.mark.parametrize("reused", [False, True])
def test_paginated_refund_transactions_are_unknown(reused: bool) -> None:
    if reused:
        body = fixture("shopify_order_already_refunded.json")
        refund = body["data"]["orders"]["nodes"][0]["refunds"][0]
        responses = []
    else:
        body = fixture("shopify_refund_created.json")
        refund = body["data"]["refundCreate"]["refund"]
        responses = [ok("shopify_order.json")]
    refund["transactions"]["pageInfo"]["hasNextPage"] = True
    responses.append(httpx.Response(200, json=body))
    stub = Stub(refund_dispatch(), responses)
    assert run(stub).status_code == 502
    assert len(stub.provider_calls) == (1 if reused else 2)
    assert not any(path.endswith("/complete") for path, *_ in stub.core_calls)


def test_multiple_matching_refunds_on_order_is_unknown() -> None:
    body = fixture("shopify_order_already_refunded.json")
    first = body["data"]["orders"]["nodes"][0]["refunds"][0]
    second = {**first, "id": "gid://shopify/Refund/9099"}
    body["data"]["orders"]["nodes"][0]["refunds"] = [first, second]
    stub = Stub(refund_dispatch(), [httpx.Response(200, json=body)])
    assert run(stub).status_code == 502
    assert len(stub.provider_calls) == 1
    assert not any(path.endswith("/complete") for path, *_ in stub.core_calls)


@pytest.mark.parametrize("lookup_status", [200, 503])
def test_expired_budget_blocks_next_provider_request(lookup_status: int) -> None:
    stub = Stub(
        refund_dispatch(), [httpx.Response(lookup_status, json=fixture("shopify_order.json"))]
    )
    times = iter([0.0, 0.0, 37.0])
    assert run(stub, clock=lambda: next(times)).status_code == 409
    assert len(stub.provider_calls) == 1
    assert not any(path.endswith("/complete") for path, *_ in stub.core_calls)


def test_write_timeout_shrinks_after_lookup() -> None:
    stub = Stub(refund_dispatch(), [ok("shopify_order.json"), ok("shopify_refund_created.json")])
    times = iter([0.0, 0.0, 33.0])
    assert run(stub, clock=lambda: next(times)).status_code == 200
    assert stub.provider_calls[1].extensions["timeout"]["read"] == 4.0


@pytest.mark.parametrize("status", [301, 400, 401, 403, 404, 408, 409, 422, 429, 503])
def test_every_non_2xx_write_status_is_unknown_outcome(status: int) -> None:
    stub = Stub(refund_dispatch(), [ok("shopify_order.json"), httpx.Response(status)])
    result = run(stub)
    assert result.status_code == 502
    assert "outcome unknown" in result.json()["detail"]
    assert len(stub.provider_calls) == 2
    assert not any(path.endswith("/complete") for path, *_ in stub.core_calls)


@pytest.mark.parametrize("status", [400, 422])
def test_gmail_non_2xx_send_status_is_unknown_outcome(status: int) -> None:
    stub = Stub(
        reply_dispatch(),
        [
            httpx.Response(200, json={"messages": [], "resultSizeEstimate": 0}),
            httpx.Response(status),
        ],
    )
    result = run(stub)
    assert result.status_code == 502
    assert "outcome unknown" in result.json()["detail"]
    assert len(stub.provider_calls) == 2
    assert not any(path.endswith("/complete") for path, *_ in stub.core_calls)


@pytest.mark.parametrize(
    "refunds",
    [None, "not-a-list", ["not-an-object"], [{"id": "gid://shopify/Refund/1", "note": 123}]],
)
def test_unparseable_shopify_order_refunds_is_unknown_outcome(refunds: Any) -> None:
    body = fixture("shopify_order.json")
    body["data"]["orders"]["nodes"][0]["refunds"] = refunds
    stub = Stub(refund_dispatch(), [httpx.Response(200, json=body)])
    result = run(stub)
    assert result.status_code == 502
    assert "outcome unknown" in result.json()["detail"]
    assert len(stub.provider_calls) == 1
    assert not any(path.endswith("/complete") for path, *_ in stub.core_calls)


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
        ([ok("shopify_order.json"), ok("shopify_refund_user_errors.json")], 502),
        ([ok("shopify_order.json"), httpx.Response(400, json={})], 502),
        ([ok("shopify_order.json"), httpx.Response(401, json={})], 502),
        ([ok("shopify_order.json"), httpx.Response(422, json={})], 502),
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


def provider_client(responses: list[httpx.Response | Exception]) -> tuple[httpx.AsyncClient, Stub]:
    stub = Stub({}, responses)
    return httpx.AsyncClient(transport=httpx.MockTransport(stub.provider)), stub


def test_search_retries_then_succeeds_and_the_write_is_sent_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waits: list[float] = []

    async def record_wait(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr("gateway.providers.sleep", record_wait)
    client, stub = provider_client(
        [httpx.Response(429), httpx.Response(502), ok("shopify_order.json")]
        + [ok("shopify_refund_created.json")]
    )
    effect = asyncio.run(
        shopify.issue_refund(client, SHOP, SHOP_TOKEN, refund_dispatch(), lambda: 5)
    )
    assert effect.external_ref == "gid://shopify/Refund/9002" and len(stub.provider_calls) == 4
    assert waits == [0.5, 1.0]  # exponential backoff between search attempts


def test_search_gives_up_after_three_attempts_without_writing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_wait(_: float) -> None:
        pass

    monkeypatch.setattr("gateway.providers.sleep", no_wait)
    client, stub = provider_client([httpx.Response(503)] * 3)
    with pytest.raises(Refused):
        asyncio.run(shopify.issue_refund(client, SHOP, SHOP_TOKEN, refund_dispatch(), lambda: 5))
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
        asyncio.run(shopify.issue_refund(client, SHOP, SHOP_TOKEN, dispatch, lambda: 5))
    assert stub.provider_calls == []


@pytest.mark.parametrize(
    ("amount", "currency"), [(210.01, "EUR"), (210, "USD")], ids=["too much", "wrong currency"]
)
def test_refund_that_does_not_fit_the_payment_is_refused_before_the_write(
    amount: float, currency: str
) -> None:
    client, stub = provider_client([ok("shopify_order.json")])
    with pytest.raises(Refused):
        asyncio.run(
            shopify.issue_refund(
                client,
                SHOP,
                SHOP_TOKEN,
                refund_dispatch(amount=amount, currency=currency),
                lambda: 5,
            )
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
        {"thread_id": None},
        {"thread_id": "not-hex!"},
        {"thread_id": "18F0ABC00000ABCD"},
        {"thread_id": "18f0abc00000abcD"},
        {"in_reply_to": None},
        {"in_reply_to": "not-a-message-id"},
        {"references": "<a@example.com>\r\nBcc: evil@example.com <order-question@example.com>"},
        {"references": "<other@example.com>"},
        {"references": "invalid-id <order-question@example.com>"},
        {"subject": "A different subject"},
        {"subject": "Fwd: Where is my order?"},
        {"subject": "Where is re: my order?"},
        {"subject": "Re: Where is my order? (urgent)"},
        {"subject": "Re: Where is   my order?"},
        {"subject": "Re:  Where is my order?"},
        {"subject": "Re:Where is my order?"},
        {"subject": "re: Where is my order?"},
        {"subject": "RE: Where is my order?"},
        {"subject": " Re: Where is my order?"},
        {"subject": "Re: Where is my order? "},
        {"thread_subject": None},
        {"thread_subject": "Re: "},
        {"thread_subject": "Where is my order?\r\nBcc: evil@example.com"},
    ],
)
def test_unsendable_draft_is_refused_without_any_call(draft: dict[str, Any]) -> None:
    dispatch = reply_dispatch()
    dispatch["draft"] = {**dispatch["draft"], **draft}
    client, stub = provider_client([])
    with pytest.raises(Refused):
        asyncio.run(gmail.send_reply(client, MAILBOX, MAIL_TOKEN, dispatch, lambda: 5))
    assert stub.provider_calls == []


@pytest.mark.parametrize(
    ("subject", "original"),
    [
        ("Where is my order?", "Where is my order?"),
        ("Re: Where is my order?", "Where is my order?"),
        ("Re: Re: Where is my order?", "Where is my order?"),
        ("Re: Where is my order?", "Re: Where is my order?"),
    ],
)
def test_reply_subject_may_differ_from_the_thread_only_by_reply_prefix(
    subject: str, original: str
) -> None:
    dispatch = reply_dispatch()
    dispatch["draft"] = {**dispatch["draft"], "subject": subject, "thread_subject": original}
    client, stub = provider_client([ok("gmail_search_empty.json"), ok("gmail_sent.json")])
    asyncio.run(gmail.send_reply(client, MAILBOX, MAIL_TOKEN, dispatch, lambda: 5))
    assert len(stub.provider_calls) == 2


@pytest.mark.parametrize(
    "body",
    [
        {"messages": [{"threadId": "18f0abc00000abcd"}], "resultSizeEstimate": 1},
        {"messages": [{"id": "", "threadId": "18f0abc00000abcd"}], "resultSizeEstimate": 1},
        {"messages": ["not-an-object"], "resultSizeEstimate": 1},
        {"resultSizeEstimate": 1},
        {"messages": [], "resultSizeEstimate": 1},
        {"messages": [], "resultSizeEstimate": 0, "nextPageToken": "tok"},
        {"resultSizeEstimate": 0, "nextPageToken": "tok"},
        {"messages": "bad", "resultSizeEstimate": 0},
        {"messages": None, "resultSizeEstimate": 0},
        {},
        {"resultSizeEstimate": False},
        {
            "messages": [
                {"id": "1", "threadId": "18f0abc00000abcd"},
                {"id": "2", "threadId": "18f0abc00000abcd"},
            ],
            "resultSizeEstimate": 2,
        },
    ],
)
def test_unparseable_gmail_duplicate_search_is_unknown_outcome(body: dict[str, Any]) -> None:
    stub = Stub(reply_dispatch(), [httpx.Response(200, json=body)])
    result = run(stub)
    assert result.status_code == 502 and "outcome unknown" in result.json()["detail"]
    assert len(stub.provider_calls) == 1
    assert not any(path.endswith("/complete") for path, *_ in stub.core_calls)


@pytest.mark.parametrize("thread", ["18f0dddd0000ffff", None])
def test_send_answer_in_another_thread_is_unknown_and_not_completed(thread: str | None) -> None:
    sent = fixture("gmail_sent.json")
    if thread is None:
        del sent["threadId"]
    else:
        sent["threadId"] = thread
    stub = Stub(reply_dispatch(), [ok("gmail_search_empty.json"), httpx.Response(200, json=sent)])
    result = run(stub)
    assert result.status_code == 502 and "outcome unknown" in result.json()["detail"]
    assert len(stub.provider_calls) == 2  # the send is not repeated
    assert not any(path.endswith("/complete") for path, *_ in stub.core_calls)


@pytest.mark.parametrize("thread", ["18f0aaaa0000cccc", None])
def test_earlier_send_found_in_another_thread_is_unknown_and_not_resent(
    thread: str | None,
) -> None:
    found = fixture("gmail_search_found.json")
    if thread is None:
        del found["messages"][0]["threadId"]
    else:
        found["messages"][0]["threadId"] = thread
    stub = Stub(reply_dispatch(), [httpx.Response(200, json=found)])
    result = run(stub)
    assert result.status_code == 502 and "outcome unknown" in result.json()["detail"]
    assert len(stub.provider_calls) == 1  # the search only: nothing is sent
    assert not any(path.endswith("/complete") for path, *_ in stub.core_calls)


def test_send_without_a_message_id_in_the_answer_is_an_unknown_outcome() -> None:
    client, _ = provider_client([ok("gmail_search_empty.json"), httpx.Response(200, json={})])
    with pytest.raises(UnknownOutcome):
        asyncio.run(gmail.send_reply(client, MAILBOX, MAIL_TOKEN, reply_dispatch(), lambda: 5))


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
