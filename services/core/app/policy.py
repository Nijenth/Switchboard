"""Evaluate declarative pack rules without executing rule content."""

import operator
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator

ORDER = {"ignore": 0, "auto": 1, "needs_approval": 2, "escalate": 3}
ROLES = {"agent", "manager", "finance", "admin"}
RISK_FLAGS = {
    "prompt_injection",
    "legal_or_chargeback",
    "legal_threat",
    "chargeback",
    "safety",
    "harassment",
    "safety_flag",
    "harassment_flag",
}
FIELDS = {
    "classification.intent_key",
    "classification.confidence",
    "classification.priority",
    "classification.language",
    "classification.sentiment",
    "classification.flags",
    "case.id",
    "case.channel",
    "case.status",
    "case.priority",
    "case.identity_verified",
    "contact.id",
    "contact.kind",
    "contact.locale",
    "tenant.id",
    "tenant.slug",
}
NUMERIC = {"gt": operator.gt, "gte": operator.ge, "lt": operator.lt, "lte": operator.le}
MISSING = object()


class InvalidRule(ValueError):
    pass


def lookup(context: dict[str, Any], path: str) -> Any:
    value: Any = context
    for key in path.split("."):
        if not isinstance(value, dict) or key not in value:
            return MISSING
        value = value[key]
    return value


def equal(left: Any, right: Any) -> bool:
    if type(left) in (int, float) and type(right) in (int, float):
        return bool(left == right)
    if type(left) is not type(right):
        return False
    if isinstance(left, list):
        return len(left) == len(right) and all(
            equal(a, b) for a, b in zip(left, right, strict=True)
        )
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            equal(value, right[key]) for key, value in left.items()
        )
    return bool(left == right)


def matches(
    condition: Any, context: dict[str, Any], fields: set[str], depth: int = 0
) -> bool | None:
    if not isinstance(condition, dict) or depth > 24:
        raise InvalidRule("Invalid policy condition")
    if len(condition) == 1:
        op, children = next(iter(condition.items()))
        if op == "not":
            result = matches(children, context, fields, depth + 1)
            return None if result is None else not result
        if op not in {"all", "any"} or not isinstance(children, list) or not children:
            raise InvalidRule("Invalid policy group")
        results = [matches(child, context, fields, depth + 1) for child in children]
        decisive = op == "any"
        return decisive if decisive in results else None if None in results else not decisive
    field, op = condition.get("field"), condition.get("op")
    if (
        set(condition) - {"field", "op", "value"}
        or not isinstance(field, str)
        or field not in fields
        or not isinstance(op, str)
    ):
        raise InvalidRule("Invalid policy field or operator")
    value = lookup(context, field)
    expected = condition.get("value", MISSING)
    if op == "exists" and (expected is MISSING or isinstance(expected, bool)):
        return (value is not MISSING) == (expected is not False)
    if op in NUMERIC and type(expected) in (int, float):
        return NUMERIC[op](value, expected) if type(value) in (int, float) else None
    if op in {"in", "not_in"} and isinstance(expected, list):
        if value is MISSING:
            return None
        found = any(equal(value, item) for item in expected)
        return found if op == "in" else not found
    if op == "contains" and expected is not MISSING:
        if isinstance(value, list):
            return any(equal(item, expected) for item in value)
        return expected in value if isinstance(value, str) and isinstance(expected, str) else None
    if op in {"eq", "ne"} and expected is not MISSING:
        if value is MISSING:
            return None
        return equal(value, expected) == (op == "eq")
    raise InvalidRule("Invalid policy operand")


@dataclass(frozen=True)
class Decision:
    decision: str
    rule_id: str
    rule_source: str
    reason: str
    required_role: str | None = None
    allowed_tools: tuple[str, ...] = ()
    queue: str | None = None
    priority: str | None = None


def rule_results(
    rules: Any, source: str, context: dict[str, Any], fields: set[str], tools: dict[str, Any]
) -> list[Decision]:
    if not isinstance(rules, list) or len(rules) > 256:
        raise InvalidRule("Invalid policy rule list")
    results = []
    seen = set()
    for rule in rules:
        if not isinstance(rule, dict) or set(rule) != {"id", "when", "then"}:
            raise InvalidRule("Invalid policy rule")
        rule_id, effect = rule["id"], rule["then"]
        if (
            not isinstance(rule_id, str)
            or not rule_id
            or rule_id in seen
            or not isinstance(effect, dict)
        ):
            raise InvalidRule("Invalid policy rule identifier")
        seen.add(rule_id)
        decision, reason = effect.get("decision"), effect.get("reason")
        role, queue = effect.get("role"), effect.get("queue")
        allowed = effect.get("allow_tools", [])
        if (
            set(effect) - {"decision", "reason", "role", "queue", "allow_tools"}
            or not isinstance(decision, str)
            or decision not in ORDER
            or not isinstance(reason, str)
            or not reason.strip()
            or (role is not None and (not isinstance(role, str) or role not in ROLES))
            or (queue is not None and (not isinstance(queue, str) or not queue))
            or not isinstance(allowed, list)
            or any(not isinstance(key, str) or key not in tools for key in allowed)
            or (allowed and decision != "auto")
        ):
            raise InvalidRule("Invalid policy effect")
        if matches(rule["when"], context, fields) is True:
            results.append(Decision(decision, rule_id, source, reason, role, tuple(allowed), queue))
    return results


def global_rules(
    pack: dict[str, Any],
    context: dict[str, Any],
    selected: set[str],
    tools: dict[str, Any],
    *,
    citations_ok: bool,
    output_ok: bool,
    budget_blocked: bool,
) -> list[Decision]:
    classification = context["classification"]
    extraction = pack.get("extraction")
    guard_flags = RISK_FLAGS.intersection(classification.get("flags", ()))
    safety = bool(guard_flags) or classification.get("intent_key") == "system_escalation"
    money_roles = {
        tools[key].get("approval_role") or "manager"
        for key in selected
        if tools[key]["risk_level"] == "money"
    }
    outbound = any(tools[key]["risk_level"] == "external_comm" for key in selected)
    thresholds = pack["thresholds"]
    confidence = classification.get("confidence")
    invalid_extraction = False
    if extraction:
        schema = pack.get("assets", {}).get("extraction_schema")
        data = context["extraction"]
        scores = context["field_confidence"]
        invalid_extraction = (
            not context["extraction_valid"]
            or schema is None
            or not Draft202012Validator(schema).is_valid(data)
            or any(
                key not in data or scores.get(key, 0) < thresholds["field_confidence"]
                for key in extraction["required_fields"]
            )
        )
    checks = (
        (
            safety,
            Decision(
                "escalate",
                "G1",
                "global",
                "A safety flag requires urgent review.",
                "admin",
                priority="urgent",
            ),
        ),
        (
            not context["case"]["identity_verified"]
            and any(tools[key]["requires_verified_identity"] for key in selected),
            Decision(
                "needs_approval",
                "G2",
                "global",
                "Verify identity before accessing restricted data.",
            ),
        ),
        (
            bool(money_roles),
            Decision(
                "needs_approval",
                "G3",
                "global",
                "Money tools require human approval.",
                next(iter(money_roles)) if len(money_roles) == 1 else "admin",
            ),
        ),
        (
            confidence is None or confidence < thresholds["auto_confidence"],
            Decision(
                "needs_approval",
                "G4",
                "global",
                "Classification confidence is below the pack threshold.",
            ),
        ),
        (
            invalid_extraction,
            Decision(
                "needs_approval",
                "G5",
                "global",
                "Required extraction evidence is missing or invalid.",
            ),
        ),
        (
            outbound and not citations_ok,
            Decision(
                "needs_approval", "G6", "global", "Outbound text requires verified citations."
            ),
        ),
        (
            outbound and not output_ok,
            Decision(
                "needs_approval",
                "G7",
                "global",
                "Outbound text requires a passing output guardrail result.",
            ),
        ),
        (
            budget_blocked,
            Decision(
                "needs_approval",
                "G8",
                "global",
                "The tenant AI budget is exhausted or unavailable.",
            ),
        ),
    )
    return [result for active, result in checks if active]


def decide(
    pack: dict[str, Any],
    context: dict[str, Any],
    tools: dict[str, Any],
    proposed: set[str],
    overrides: Any = (),
    *,
    citations_ok: bool = False,
    output_ok: bool = False,
    budget_blocked: bool = False,
) -> Decision:
    fields = FIELDS | {
        f"extraction.{key}"
        for key in pack.get("assets", {}).get("extraction_schema", {}).get("properties", {})
    }
    fields |= {f"checks.{key}" for tool in tools.values() for key in tool.get("check_fields", ())}
    default = Decision(
        "needs_approval", "DEFAULT_REVIEW", "default", "No rule authorized automatic handling."
    )
    try:
        pack_matches = rule_results(pack["rules"]["rules"], "pack", context, fields, tools)
        candidate = next(iter(pack_matches), default)
        tenant_matches = rule_results(
            list(overrides) if isinstance(overrides, tuple) else overrides,
            "tenant",
            context,
            fields,
            tools,
        )
    except InvalidRule:
        return Decision(
            "escalate",
            "INVALID_POLICY",
            "global",
            "The published policy configuration is invalid.",
            "admin",
            priority="urgent",
        )
    selected = proposed | set(candidate.allowed_tools)
    guards = global_rules(
        pack,
        context,
        selected,
        tools,
        citations_ok=citations_ok,
        output_ok=output_ok,
        budget_blocked=budget_blocked,
    )
    if guards:
        candidate = max([*guards, candidate], key=lambda item: ORDER[item.decision])
    for override in tenant_matches:
        if override.decision in {"needs_approval", "escalate"} and (
            ORDER[override.decision] > ORDER[candidate.decision]
        ):
            candidate = override
    # Retain the strongest required approval even when a different global rule wins.
    if candidate.decision == "needs_approval":
        roles = {
            item.required_role
            for item in [candidate, *guards, *tenant_matches]
            if item.decision == "needs_approval" and item.required_role
        }
        if len(roles) > 1:
            return Decision(
                candidate.decision,
                candidate.rule_id,
                candidate.rule_source,
                candidate.reason,
                "admin",
                queue=candidate.queue,
            )
        if roles and candidate.required_role is None:
            return Decision(
                candidate.decision,
                candidate.rule_id,
                candidate.rule_source,
                candidate.reason,
                next(iter(roles)),
                queue=candidate.queue,
            )
    return candidate
