from __future__ import annotations

import hashlib
import json
from collections.abc import Hashable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator
from yaml.nodes import MappingNode

SCHEMAS = Path(__file__).with_name("pack_schemas")
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


class PackError(ValueError):
    pass


class UniqueLoader(yaml.SafeLoader):
    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(yaml.AliasEvent):
            raise PackError("YAML aliases are unsupported")
        return super().compose_node(parent, index)

    def construct_mapping(self, node: MappingNode, deep: bool = False) -> dict[Hashable, Any]:
        result = super().construct_mapping(node, deep)
        if len(result) != len(node.value) or any(not isinstance(key, str) for key in result):
            raise PackError("YAML object keys must be unique strings")
        return result


@dataclass(frozen=True)
class Pack:
    config: dict[str, Any]
    tools: list[dict[str, Any]]
    rules: dict[str, Any]
    cases: list[dict[str, Any]]
    knowledge: list[tuple[dict[str, str], str]]
    checksum: str
    assets: dict[str, Any]


def validate(value: Any, schema_name: str, label: str) -> None:
    schema = json.loads((SCHEMAS / f"{schema_name}.json").read_text(encoding="utf-8"))
    error = next(Draft202012Validator(schema).iter_errors(value), None)
    if error is not None:
        location = ".".join(map(str, error.absolute_path)) or "root"
        raise PackError(f"{label}:{location}: invalid {schema_name} structure ({error.validator})")


def read_yaml(path: Path, schema: str) -> dict[str, Any]:
    try:
        value = yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueLoader)
        json.dumps(value, allow_nan=False)
        validate(value, schema, str(path))
        return dict(value)
    except (OSError, ValueError, TypeError, yaml.YAMLError, RecursionError) as error:
        if isinstance(error, PackError) and str(error).startswith(str(path)):
            raise
        raise PackError(f"{path}: invalid YAML or unsupported data") from None


def unique(rows: list[dict[str, Any]], key: str, label: str) -> dict[str, dict[str, Any]]:
    result = {row[key]: row for row in rows}
    if len(result) != len(rows):
        raise PackError(f"{label}: duplicate {key}")
    return result


def reference(root: Path, name: str, folder: str, suffix: str) -> Path:
    path = root / name
    if (
        "\\" in name
        or not name.startswith(folder + "/")
        or path.suffix != suffix
        or not path.resolve().is_relative_to(root.resolve() / folder)
        or not path.is_file()
    ):
        raise PackError(f"{root.name}: invalid or missing reference {name}")
    return path


def contract(schema: dict[str, Any], label: str) -> set[str]:
    from jsonschema.exceptions import SchemaError

    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError:
        raise PackError(f"{label}: invalid JSON Schema") from None
    if schema.get("type") != "object":
        raise PackError(f"{label}: contract must describe an object")
    return set(schema.get("properties", {}))


def check_condition(
    condition: dict[str, Any], fields: set[str], intents: set[str], label: str
) -> None:
    if "field" not in condition:
        op, children = next(iter(condition.items()))
        for child in [children] if op == "not" else children:
            check_condition(child, fields, intents, label)
        return
    field, op = condition["field"], condition["op"]
    value = condition.get("value")
    if field not in fields:
        raise PackError(f"{label}: unknown condition field {field}")
    if op == "exists":
        if "value" in condition and not isinstance(value, bool):
            raise PackError(f"{label}: exists value must be boolean")
        return
    if "value" not in condition or (op in {"in", "not_in"} and not isinstance(value, list)):
        raise PackError(f"{label}: invalid condition operand for {op}")
    if op in {"gt", "gte", "lt", "lte"} and type(value) not in {int, float}:
        raise PackError(f"{label}: numeric condition requires a number")
    if field == "classification.intent_key" and op in {"eq", "ne", "in", "not_in"}:
        values = value if isinstance(value, list) and op in {"in", "not_in"} else [value]
        if any(not isinstance(item, str) or item not in intents for item in values):
            raise PackError(f"{label}: unknown condition intent")


def check_lookups(
    root: Path,
    config: dict[str, Any],
    selected: dict[str, dict[str, Any]],
    registry: dict[str, dict[str, Any]],
    fields: set[str],
) -> None:
    """Lookups must be selected read tools whose declared inputs map onto known fields."""
    label = f"{root}/pack.yaml:lookups"
    seen = set()
    outputs: set[str] = set()
    for entry in config.get("lookups", []):
        key = entry["tool"]
        tool = registry.get(key)
        if key not in selected or tool is None or tool["risk_level"] != "read":
            raise PackError(f"{label}: {key} must be a read tool selected in tools.yaml")
        if key in seen:
            raise PackError(f"{label}: duplicate lookup {key}")
        seen.add(key)
        schema = tool.get("input_schema")
        if not isinstance(schema, dict) or "output_schema" not in tool:
            raise PackError(f"{label}: {key} needs registry input and output schemas")
        expected = set(schema.get("properties", {}))
        if set(entry["input"]) != expected or not set(schema.get("required", [])) <= expected:
            raise PackError(f"{label}: {key} inputs must match its input schema")
        for source in entry["input"].values():
            if source.startswith("extraction.") and source not in fields:
                raise PackError(f"{label}: unknown lookup source {source}")
        # Lookup results merge into flat checks.* fields, so names must not collide.
        produced = set(tool["output_schema"].get("properties", {}))
        if produced & outputs:
            raise PackError(f"{label}: {key} output fields collide with an earlier lookup")
        outputs |= produced


def read_contract(path: Path) -> set[str]:
    try:
        return contract(json.loads(path.read_text(encoding="utf-8")), str(path))
    except (OSError, ValueError, TypeError, AttributeError):
        raise PackError(f"{path}: invalid JSON Schema") from None


def read_cases(
    root: Path, config: dict[str, Any], intents: set[str], tools: set[str]
) -> list[dict[str, Any]]:
    if "evals" not in config:
        return []
    path = reference(root, config["evals"], "evals", ".jsonl")
    cases = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        label = f"{path}:{number}"
        try:
            case = json.loads(line)
            json.dumps(case, allow_nan=False)
        except ValueError:
            raise PackError(f"{label}: invalid JSON") from None
        validate(case, "eval", label)
        if (
            case["expected_intent"] not in intents
            or case["channel"] not in config["input_channels"]
        ):
            raise PackError(f"{label}: unknown eval intent or channel")
        expected = set(case.get("expected_tools", []))
        forbidden = set(case.get("forbidden_tools", []))
        if not (expected | forbidden) <= tools or expected & forbidden:
            raise PackError(f"{label}: unknown or conflicting eval tool")
        if "document_fixture" in case:
            name = case["document_fixture"]
            reference(root, name, "evals", Path(name).suffix)
        cases.append(case)
    unique(cases, "case_key", str(path))
    return cases


def pack_files(root: Path) -> list[tuple[str, str]]:
    files = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise PackError(f"{root}: pack files cannot be symlinks")
        if path.is_file() and path != root / "README.md":
            files.append(
                (path.relative_to(root).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest())
            )
    return files


def read_pack(root: Path, registry: dict[str, dict[str, Any]]) -> Pack:
    files = pack_files(root)
    config = read_yaml(root / "pack.yaml", "pack")
    if config["key"] != root.name:
        raise PackError(f"{root}: pack key must match its directory")
    intents = set(unique(config["intents"], "key", str(root / "pack.yaml")))
    if "system_escalation" in intents:
        raise PackError(f"{root}: system_escalation is a reserved intent")
    tools = read_yaml(root / "tools.yaml", "tools")["tools"]
    selected = unique(tools, "key", str(root / "tools.yaml"))
    fields = set(FIELDS)
    for key, spec in selected.items():
        if key not in registry:
            raise PackError(f"{root}/tools.yaml: unknown tool {key}")
        tool = registry[key]
        if tool["risk_level"] == "money" and (
            "approval" not in spec or spec["approval"].get("above_amount", 0) != 0
        ):
            raise PackError(
                f"{root}/tools.yaml: money tool {key} requires approval for every amount"
            )
        fields.update(
            f"checks.{name}" for name in tool.get("output_schema", {}).get("properties", {})
        )
    assets: dict[str, Any] = {"prompts": {}}
    if extraction := config.get("extraction"):
        schema_path = reference(root, extraction["schema"], "schemas", ".json")
        properties = read_contract(schema_path)
        assets["extraction_schema"] = json.loads(schema_path.read_text(encoding="utf-8"))
        if not set(extraction["required_fields"]) <= properties:
            raise PackError(f"{root}/pack.yaml: unknown required extraction field")
        fields.update(f"extraction.{name}" for name in properties)
    check_lookups(root, config, selected, registry, fields)
    for name in config.get("output_schemas", {}).values():
        read_contract(reference(root, name, "schemas", ".json"))
    for step, name in config.get("prompts", {}).items():
        assets["prompts"][step] = reference(root, name, "prompts", ".md").read_text(
            encoding="utf-8"
        )
    knowledge = [
        (
            entry,
            reference(root, entry["file"], "knowledge", ".md").read_text(encoding="utf-8"),
        )
        for entry in config.get("knowledge", [])
    ]
    unique([entry for entry, _ in knowledge], "title", str(root / "pack.yaml"))
    rules = read_yaml(root / "rules.yaml", "rules")
    unique(rules["rules"], "id", str(root / "rules.yaml"))
    for rule in rules["rules"]:
        label = f"{root}/rules.yaml:{rule['id']}"
        check_condition(rule["when"], fields, intents, label)
        effect = rule["then"]
        for key in effect.get("allow_tools", []):
            if (
                key not in selected
                or registry[key]["risk_level"] == "money"
                or effect["decision"] != "auto"
            ):
                raise PackError(f"{label}: invalid automatic tool {key}")
    cases = read_cases(root, config, intents, set(selected))
    if files != pack_files(root):
        raise PackError(f"{root}: pack changed during validation")
    digest = json.dumps([files, [registry[key] for key in sorted(selected)]], sort_keys=True)
    return Pack(
        config,
        tools,
        rules,
        cases,
        knowledge,
        hashlib.sha256(digest.encode()).hexdigest(),
        assets,
    )


def read_catalog(root: Path) -> tuple[dict[str, dict[str, Any]], list[Pack]]:
    registry = unique(
        read_yaml(root / "tool_registry.yaml", "registry")["tools"],
        "key",
        "tool_registry.yaml",
    )
    for key, tool in registry.items():
        for direction in ("input", "output"):
            name = f"{direction}_schema"
            if name in tool:
                contract(tool[name], f"tool_registry.yaml:{key}.{name}")
    packs = [read_pack(path, registry) for path in sorted(root.iterdir()) if path.is_dir()]
    if not packs:
        raise PackError(f"{root}: no packs found")
    return registry, packs
