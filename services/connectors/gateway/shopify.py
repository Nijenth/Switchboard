"""order_refund_issue on Shopify's Admin GraphQL API.

Request and response shapes follow the public API documentation and have not
been exercised against a real store (ADR 0032, "Known limits").
"""

import re
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from gateway.providers import Effect, Refused, UnknownOutcome, search, write

ORDER_NUMBER = re.compile(r"[A-Za-z0-9#-]{1,40}")
IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9:._-]{1,200}")

FIND_ORDER = """
query SwitchboardFindOrder($query: String!) {
  orders(first: 2, query: $query) {
    nodes {
      id
      name
      refunds { id note }
      transactions {
        id kind status gateway
        amountSet { shopMoney { amount currencyCode } }
      }
    }
  }
}
"""

CREATE_REFUND = """
mutation SwitchboardRefund($input: RefundInput!) {
  refundCreate(input: $input) {
    refund { id }
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


def issue_refund(
    client: httpx.Client, endpoint: str, token: str, dispatch: dict[str, Any], timeout: float
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

    found = search(
        client,
        "POST",
        endpoint,
        timeout=timeout,
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
        for refund in order["refunds"]:
            if marker(key) in (refund.get("note") or "").split():
                return Effect(external_ref=str(refund["id"]), reused=True)
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

    created = write(
        client,
        "POST",
        endpoint,
        timeout=timeout,
        headers=headers,
        json={
            "query": CREATE_REFUND,
            "variables": {
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
                }
            },
        },
    )
    try:
        result = _graphql_data(created)["refundCreate"]
        if result["userErrors"]:
            # Shopify validated and rejected the input. Nothing was refunded.
            raise Refused("Shopify rejected the refund input")
        return Effect(external_ref=str(result["refund"]["id"]), reused=False)
    except (KeyError, TypeError, ValueError):
        raise UnknownOutcome("Refund response had an unexpected shape") from None
