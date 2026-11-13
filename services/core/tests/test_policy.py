from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.policy import InvalidRule, decide, global_rules, matches

PACK_DIR = Path(__file__).resolve().parents[3] / "packs" / "ecommerce_support"
PACK: dict[str, Any] = yaml.safe_load((PACK_DIR / "pack.yaml").read_text(encoding="utf-8"))
PACK["rules"] = yaml.safe_load((PACK_DIR / "rules.yaml").read_text(encoding="utf-8"))
PACK["assets"] = {
    "extraction_schema": {"type": "object", "properties": {"order_number": {"type": "string"}}}
}
TOOLS = {
    "kb_search": {"risk_level": "read", "requires_verified_identity": False},
    "order_lookup": {"risk_level": "read", "requires_verified_identity": True},
    "refund_issue": {
        "risk_level": "money",
        "requires_verified_identity": True,
        "approval_role": "manager",
    },
    "send_reply": {"risk_level": "external_comm", "requires_verified_identity": False},
}
CONTEXT: dict[str, Any] = {
    "classification": {"intent_key": "product_question", "confidence": 0.95, "flags": []},
    "case": {"identity_verified": True},
    "extraction": {},
    "extraction_valid": True,
    "field_confidence": {},
}


@pytest.mark.parametrize(
    ("op", "yes", "no"),
    [
        ("eq", 3, True),
        ("ne", True, 3),
        ("gt", 2, 3),
        ("gte", 3, 4),
        ("lt", 4, 3),
        ("lte", 3, 2),
        ("in", [3], [True]),
        ("not_in", [True], [3]),
        ("exists", True, False),
    ],
)
def test_leaf_operators(op: str, yes: object, no: object) -> None:
    for operand, expected in ((yes, True), (no, False)):
        assert (
            matches(
                {"field": "checks.x", "op": op, "value": operand},
                {"checks": {"x": 3}},
                {"checks.x"},
            )
            is expected
        )


def test_nested_conditions_and_missing_data() -> None:
    context = {"checks": {"present": [3, 4]}}
    fields = {"checks.present", "checks.absent"}
    yes = {"field": "checks.present", "op": "contains", "value": 3}
    no = {**yes, "value": 5}
    missing = {"field": "checks.absent", "op": "eq", "value": 3}
    assert matches({"all": [yes, {"not": no}]}, context, fields) is True
    assert matches({"any": [no, yes]}, context, fields) is True
    assert matches({"not": missing}, context, fields) is None
    assert matches({"all": [yes, missing]}, context, fields) is None
    assert matches({"any": [no, missing]}, context, fields) is None
    with pytest.raises(InvalidRule):
        matches(
            {"any": [yes, {"field": "checks.unknown", "op": "eq", "value": 3}]}, context, fields
        )


@pytest.mark.parametrize("rule", [f"G{number}" for number in range(1, 9)])
def test_global_rule_matches_and_does_not_match(rule: str) -> None:
    context = deepcopy(CONTEXT)
    pack = deepcopy(PACK)
    selected: set[str] = set()
    evidence = {"citations_ok": True, "output_ok": True, "budget_blocked": False}
    assert global_rules(pack, context, selected, TOOLS, **evidence) == []
    match rule:
        case "G1":
            context["classification"]["intent_key"] = "system_escalation"
        case "G2":
            context["case"]["identity_verified"] = False
            selected.add("order_lookup")
        case "G3":
            selected.add("refund_issue")
        case "G4":
            context["classification"]["confidence"] = 0.79
        case "G5":
            pack["extraction"]["required_fields"] = ["order_number"]
            context["extraction"] = {"order_number": "NW-1"}
            context["field_confidence"] = {"order_number": 0.89}
        case "G6":
            selected.add("send_reply")
            evidence["citations_ok"] = False
        case "G7":
            selected.add("send_reply")
            evidence["output_ok"] = False
        case "G8":
            evidence["budget_blocked"] = True
    assert [item.rule_id for item in global_rules(pack, context, selected, TOOLS, **evidence)] == [
        rule
    ]


@pytest.mark.parametrize(
    ("intent", "rule_id", "decision"),
    [
        ("spam_or_auto", "SPAM_OR_AUTO", "ignore"),
        ("shipping_delay", "SHIPPING_DELAY", "needs_approval"),
        ("returns_refunds", "RETURNS_REFUNDS", "needs_approval"),
        ("payment_issue", "PAYMENT_ISSUE", "needs_approval"),
        ("order_status", "ORDER_STATUS", "auto"),
        ("product_question", "PRODUCT_QUESTION", "auto"),
    ],
)
def test_each_ecommerce_rule(intent: str, rule_id: str, decision: str) -> None:
    context = deepcopy(CONTEXT)
    context["classification"]["intent_key"] = intent
    result = decide(PACK, context, TOOLS, set(), citations_ok=True, output_ok=True)
    assert (result.rule_id, result.decision) == (rule_id, decision)
    context["classification"]["intent_key"] = "other"
    assert decide(PACK, context, TOOLS, set()).rule_id == "DEFAULT_REVIEW"


def test_tenant_can_only_tighten_and_money_role_survives_other_guards() -> None:
    rule = {
        "id": "TENANT",
        "when": {"field": "case.identity_verified", "op": "eq", "value": True},
        "then": {"decision": "escalate", "reason": "Tenant requests escalation."},
    }
    assert decide(PACK, CONTEXT, TOOLS, set(), [rule]).rule_source == "tenant"
    context = deepcopy(CONTEXT)
    context["case"]["identity_verified"] = False
    assert decide(PACK, context, TOOLS, set(), [rule]).rule_id == "G6"
    rule["then"]["decision"] = "auto"
    assert decide(PACK, CONTEXT, TOOLS, set(), [rule]).rule_id == "G6"
    result = decide(PACK, context, TOOLS, {"refund_issue"})
    assert (result.rule_id, result.required_role, result.allowed_tools) == ("G2", "manager", ())
    context["classification"]["flags"] = ["harassment"]
    assert decide(PACK, context, TOOLS, {"refund_issue"}).rule_id == "G1"
