import json
from pathlib import Path
from shutil import copytree
from typing import Any

import pytest
import yaml

from scripts.pack_validation import PackError, read_catalog

PACKS = Path(__file__).resolve().parents[2] / "packs"


def test_all_released_packs_validate() -> None:
    tools, packs = read_catalog(PACKS)
    assert len(tools) == 17
    assert {pack.config["key"] for pack in packs} == {
        "ecommerce_support",
        "sales_leads",
        "invoice_processing",
    }
    assert len(packs[0].cases) == 10


@pytest.mark.parametrize(
    ("filename", "mutation", "diagnostic"),
    [
        ("pack.yaml", "bad_version", "invalid pack structure"),
        ("pack.yaml", "key_mismatch", "match its directory"),
        ("pack.yaml", "duplicate_intent", "duplicate key"),
        ("pack.yaml", "missing_prompt", "invalid or missing reference"),
        ("pack.yaml", "bad_extraction", "unknown required extraction field"),
        ("tools.yaml", "unknown_tool", "unknown tool"),
        ("tools.yaml", "missing_approval", "requires approval"),
        ("tools.yaml", "threshold_bypass", "requires approval"),
        ("rules.yaml", "unknown_field", "unknown condition field"),
        ("rules.yaml", "unknown_operator", "invalid rules structure"),
        ("rules.yaml", "mixed_operators", "invalid rules structure"),
        ("rules.yaml", "auto_money", "invalid automatic tool"),
        ("rules.yaml", "duplicate_rule", "duplicate id"),
        ("rules.yaml", "unknown_intent", "unknown condition intent"),
        ("rules.yaml", "bad_operand", "invalid condition operand"),
        ("schemas/order_reference.v1.json", "invalid_contract", "invalid JSON Schema"),
    ],
)
def test_catalog_refuses_invalid_fixture(
    tmp_path: Path,
    filename: str,
    mutation: str,
    diagnostic: str,
) -> None:
    root = copytree(PACKS, tmp_path / "packs")
    path = root / "ecommerce_support" / filename
    data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    match mutation:
        case "bad_version":
            data["version"] = 0
        case "key_mismatch":
            data["key"] = "other_pack"
        case "duplicate_intent":
            data["intents"].append({**data["intents"][0], "description": "duplicate"})
        case "missing_prompt":
            data["prompts"]["classify"] = "prompts/missing.md"
        case "bad_extraction":
            data["extraction"]["required_fields"] = ["missing"]
        case "unknown_tool":
            data["tools"][0]["key"] = "unregistered"
        case "missing_approval" | "threshold_bypass":
            refund = next(row for row in data["tools"] if row["key"] == "refund_issue")
            if mutation == "missing_approval":
                refund.pop("approval")
            else:
                refund["approval"]["above_amount"] = 1
        case "unknown_field":
            data["rules"][0]["when"]["field"] = "tenant.unregistered_field"
        case "unknown_operator":
            data["rules"][0]["when"]["op"] = "execute"
        case "mixed_operators":
            data["rules"][0]["when"]["not"] = data["rules"][1]["when"]
        case "auto_money":
            data["rules"][0]["then"] = {
                "decision": "auto",
                "reason": "invalid",
                "allow_tools": ["refund_issue"],
            }
        case "duplicate_rule":
            data["rules"].append(
                {**data["rules"][0], "then": {"decision": "ignore", "reason": "dup"}}
            )
        case "unknown_intent":
            data["rules"][0]["when"]["value"] = "unknown"
        case "bad_operand":
            data["rules"][0]["when"]["op"] = "in"
        case "invalid_contract":
            data["properties"]["order_number"]["type"] = "invalid"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(PackError, match=diagnostic):
        read_catalog(root)


@pytest.mark.parametrize("bad_yaml", ["key: one\nkey: two\n", "key: &key abc\nname: *key\n"])
def test_loader_rejects_ambiguous_yaml(tmp_path: Path, bad_yaml: str) -> None:
    root = copytree(PACKS, tmp_path / "packs")
    (root / "ecommerce_support" / "pack.yaml").write_text(bad_yaml, encoding="utf-8")
    with pytest.raises(PackError, match="invalid YAML"):
        read_catalog(root)


def test_loader_rejects_unknown_eval_tool(tmp_path: Path) -> None:
    root = copytree(PACKS, tmp_path / "packs")
    path = root / "ecommerce_support" / "evals" / "cases.jsonl"
    cases = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    cases[0]["expected_tools"] = ["unknown"]
    path.write_text("\n".join(map(json.dumps, cases)), encoding="utf-8")
    with pytest.raises(PackError, match="unknown or conflicting eval tool"):
        read_catalog(root)
