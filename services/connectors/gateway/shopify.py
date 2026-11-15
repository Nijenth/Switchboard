"""order_refund_issue on Shopify's Admin GraphQL API.

Request and response shapes follow the public API documentation and have not
been exercised against a real store (ADR 0032, "Known limits").
"""

import re
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from gateway.providers import (
    Effect,
    ProviderFailed,
    Refused,
    TimeBudget,
    UnknownOutcome,
    search,
    write,
)

ORDER_NUMBER = re.compile(r"[A-Za-z0-9#-]{1,40}")
IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9:._-]{1,200}")

FIND_ORDER = """
query SwitchboardFindOrder($query: String!) {
  orders(first: 2, query: $query) {
    nodes {
      id
      name
      refunds {
        id
        note
        transactions(first: 10) { nodes { status } pageInfo { hasNextPage } }
      }
      transactions {
        id kind status gateway
        amountSet { shopMoney { amount currencyCode } }
      }
    }
  }
}
"""

CREATE_REFUND = """
mutation SwitchboardRefund($input: RefundInput!, $key: String!) {
  refundCreate(input: $input) @idempotent(key: $key) {
    refund {
      id
      transactions(first: 10) { nodes { status } pageInfo { hasNextPage } }
    }
    userErrors { field message }
  }
}
"""


def marker(idempotency_key: str) -> str:
    return f"switchboard:{idempotency_key}"


def _graphql_data(body: dict[str, Any]) -> dict[str, Any]:
    data = body.get("data")
    if body.get("errors") or not isinstance(data, dict):
        raise ValueError("GraphQL error")
    return data


def _no_effect(error: Any) -> bool:
    """True only for a user error that names the input field it rejects.

    Shopify documents refundCreate's userErrors as ``field`` and ``message``.
    Its idempotency errors (request in progress, key reused with other
    parameters) carry ``field: null`` and a ``code``; an earlier request with
    the same key may have taken effect, so those never mean nothing happened.
    A ``code`` is not requested and none is known to mean a plain validation
    error, so any error that carries one is treated the same way.
    """
    if not isinstance(error, dict) or error.get("code") is not None:
        return False
    path = error.get("field")
    return (
        isinstance(path, list)
        and bool(path)
        and all(isinstance(part, str) and part for part in path)
        and isinstance(error.get("message"), str)
    )


def _refund_effect(refund: dict[str, Any], *, reused: bool) -> Effect:
    try:
        external_ref = refund["id"]
        transactions = refund["transactions"]
        statuses = {transaction["status"] for transaction in transactions["nodes"]}
        if (
            not isinstance(external_ref, str)
            or not external_ref
            or not statuses
            or transactions["pageInfo"]["hasNextPage"] is not False
        ):
            raise ValueError("Incomplete refund evidence")
    except (KeyError, TypeError, ValueError):
        raise UnknownOutcome("Shopify refund evidence was incomplete") from None
    if statuses == {"SUCCESS"}:
        return Effect(external_ref=external_ref, reused=reused)
    if statuses <= {"ERROR", "FAILURE"}:
        raise ProviderFailed("Shopify refund transactions failed")
    raise UnknownOutcome("Shopify refund has an unsettled or mixed transaction status")


async def issue_refund(
    client: httpx.AsyncClient,
    endpoint: str,
    token: str,
    dispatch: dict[str, Any],
    budget: TimeBudget,
) -> Effect:
    """Refund the approved amount once. ``dispatch`` is core's consumed-grant payload."""
    order_number = (dispatch.get("input") or {}).get("order_number")
    key = dispatch.get("idempotency_key")
    currency = dispatch.get("currency")
    try:
        amount = Decimal(str(dispatch.get("amount")))
    except InvalidOperation:
        raise Refused("Approved payload has no valid amount") from None
    if (
        not isinstance(order_number, str)
        or not ORDER_NUMBER.fullmatch(order_number)
        or not isinstance(key, str)
        or not IDEMPOTENCY_KEY.fullmatch(key)
        or not isinstance(currency, str)
        or not amount.is_finite()
        or amount <= 0
    ):
        raise Refused("Approved payload is missing an order, key, currency or positive amount")
    headers = {"X-Shopify-Access-Token": token}

    found = await search(
        client,
        "POST",
        endpoint,
        budget=budget,
        headers=headers,
        json={"query": FIND_ORDER, "variables": {"query": f'name:"{order_number}"'}},
    )
    try:
        nodes = _graphql_data(found)["orders"]["nodes"]
        # The search is a prefix match on some stores, so require the exact name.
        orders = [node for node in nodes if node["name"].lstrip("#") == order_number.lstrip("#")]
        if len(orders) != 1:
            raise Refused("Order lookup did not return exactly one order")
        order = orders[0]
        matches = [
            refund
            for refund in order["refunds"]
            if marker(key) in (refund.get("note") or "").split()
        ]
        if len(matches) > 1:
            raise UnknownOutcome("Multiple Shopify refunds matched the idempotency key")
        if matches:
            return _refund_effect(matches[0], reused=True)
        payments = [
            item
            for item in order["transactions"]
            if item["kind"] in ("SALE", "CAPTURE") and item["status"] == "SUCCESS"
        ]
        if len(payments) != 1:
            raise Refused("Order does not have exactly one successful payment to refund")
        payment = payments[0]
        money = payment["amountSet"]["shopMoney"]
        if money["currencyCode"] != currency or Decimal(str(money["amount"])) < amount:
            raise Refused("Approved amount or currency does not fit the original payment")
        order_id, parent_id, gateway = order["id"], payment["id"], payment["gateway"]
    except (KeyError, TypeError, ValueError, InvalidOperation, AttributeError):
        raise Refused("Order lookup returned an unexpected shape") from None

    created = await write(
        client,
        "POST",
        endpoint,
        budget=budget,
        headers=headers,
        json={
            "query": CREATE_REFUND,
            "variables": {
                "key": key,
                "input": {
                    "orderId": order_id,
                    "note": f"Switchboard refund {marker(key)}",
                    "notify": False,
                    "transactions": [
                        {
                            "orderId": order_id,
                            "parentId": parent_id,
                            "amount": str(amount),
                            "gateway": gateway,
                            "kind": "REFUND",
                        }
                    ],
                },
            },
        },
    )
    try:
        result = _graphql_data(created)["refundCreate"]
        refund, errors = result["refund"], result["userErrors"]
        if not isinstance(errors, list):
            raise TypeError("Invalid user errors")
        if errors:
            if refund is not None:
                raise UnknownOutcome("Shopify returned a refund record with user errors")
            if not all(_no_effect(error) for error in errors):
                raise UnknownOutcome("Shopify user error does not prove that nothing happened")
            raise Refused("Shopify rejected the refund input")
        return _refund_effect(refund, reused=False)
    except (KeyError, TypeError, ValueError):
        raise UnknownOutcome("Refund response had an unexpected shape") from None
