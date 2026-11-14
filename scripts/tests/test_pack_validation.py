import json
import re
from pathlib import Path
from shutil import copytree
from typing import Any

import pytest
import yaml

from scripts.pack_validation import PackError, read_catalog

PACKS = Path(__file__).resolve().parents[2] / "packs"


def test_all_released_packs_validate() -> None:
    tools, packs = read_catalog(PACKS)
    assert len(tools) == 20
    assert {pack.config["key"] for pack in packs} == {
        "ecommerce_support",
        "sales_leads",
        "invoice_processing",
    }
    cases = packs[0].cases
    assert len(cases) == 61
    refund_pack = next(
        pack
        for pack in packs
        if (pack.config["key"], pack.config["version"]) == ("ecommerce_support", 2)
    )
    assert len(refund_pack.cases) == 66
    assert sum(case["case_key"].startswith("ecom-refund-v2-") for case in refund_pack.cases) == 5
    for tag, minimum in {"non_english": 5, "injection": 8, "typo": 1, "multi_intent": 1}.items():
        assert sum(tag in case.get("tags", []) for case in cases) >= minimum


def test_ecommerce_v3_preserves_v2_and_covers_demo_orders() -> None:
    v2 = PACKS / "ecommerce_support.v2"
    v3 = PACKS / "ecommerce_support.v3"
    eval_path = "evals/cases.jsonl"
    original = (v2 / eval_path).read_bytes()
    expanded = (v3 / eval_path).read_bytes()
    assert expanded.startswith(original)
    for source in v2.rglob("*"):
        if source.is_dir():
            continue
        relative = source.relative_to(v2)
        if relative.as_posix() in {"README.md", eval_path}:
            continue
        expected = source.read_bytes()
        if relative.as_posix() == "pack.yaml":
            expected = expected.replace(b"version: 2\n", b"version: 3\n", 1)
        assert (v3 / relative).read_bytes() == expected

    cases = [json.loads(line) for line in expanded.splitlines()]
    new_cases = [json.loads(line) for line in expanded[len(original) :].splitlines()]
    assert len(new_cases) >= 44
    assert all(case.get("notes") for case in new_cases)
    assert all(
        case["sender_address"].rsplit("@", 1)[-1] in {"example.com", "example.test"}
        for case in new_cases
    )
    language_tags = {"de", "es", "fr", "pt"}
    assert len({tag for case in new_cases for tag in case["tags"] if tag in language_tags}) >= 3
    seed = (PACKS.parent / "db/seeds/001_demo_seed.sql").read_text(encoding="utf-8")
    direct_orders = set(re.findall(r"'(NW-\d+)'", seed))
    generated_orders = {f"NW-{number}" for number in re.findall(r"\((\d{4}),'[a-z_]+'\)", seed)}
    assert set(re.findall(r"NW-\d+", expanded.decode())) <= direct_orders | generated_orders
    minimums = {
        "typo": 8,
        "multi_intent": 6,
        "non_english": 10,
        "web_chat": 10,
        "spam_or_auto": 5,
        "expected_auto": 15,
        "order_status": 14,
        "shipping_delay": 14,
        "injection": 14,
        "unverified": 12,
        "must_escalate": 14,
        "money": 16,
    }
    groups = [
        set(case["tags"])
        | {
            case["channel"],
            case["expected_intent"],
            f"expected_{case['expected_decision']}",
        }
        for case in cases
    ]
    for tag, minimum in minimums.items():
        assert sum(tag in group for group in groups) >= minimum, tag


@pytest.mark.parametrize(
    ("filename", "mutation", "diagnostic"),
    [
        ("pack.yaml", "bad_version", "invalid pack structure"),
        ("pack.yaml", "key_mismatch", "match its directory"),
        ("pack.yaml", "duplicate_intent", "duplicate key"),
        ("pack.yaml", "missing_prompt", "invalid or missing reference"),
        ("pack.yaml", "readme_as_prompt", "invalid or missing reference"),
        ("pack.yaml", "bad_extraction", "unknown required extraction field"),
        ("pack.yaml", "lookup_not_read", "must be a read tool"),
        ("pack.yaml", "lookup_unknown_source", "unknown lookup source"),
        ("pack.yaml", "lookup_input_mismatch", "inputs must match its input schema"),
        ("pack.yaml", "lookup_duplicate", "duplicate lookup"),
        ("pack.yaml", "lookup_bad_source_syntax", "invalid pack structure"),
        ("pack.yaml", "evidence_unknown_field", "evidence uses unknown fields"),
        ("pack.yaml", "evidence_quotes_message", "evidence cannot quote message fields"),
        ("pack.yaml", "evidence_too_long", "invalid pack structure"),
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
        case "readme_as_prompt":
            data["prompts"]["classify"] = "prompts/../README.md"
        case "bad_extraction":
            data["extraction"]["required_fields"] = ["missing"]
        case "lookup_not_read":
            data["lookups"][1]["tool"] = "refund_issue"
        case "lookup_unknown_source":
            data["lookups"][1]["input"]["order_number"] = "extraction.missing"
        case "lookup_input_mismatch":
            data["lookups"][0]["input"].pop("sender_email")
        case "lookup_duplicate":
            data["lookups"].append(data["lookups"][0])
        case "lookup_bad_source_syntax":
            data["lookups"][1]["input"]["order_number"] = "case.id"
        case "evidence_unknown_field":
            data["lookups"][1]["evidence"] = "Order {order_number} costs {price}."
        case "evidence_quotes_message":
            data["lookups"][0]["evidence"] = "We emailed {sender_email}."
        case "evidence_too_long":
            data["lookups"][1]["evidence"] = "x" * 301
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


def test_pack_checksum_excludes_only_top_level_readme(tmp_path: Path) -> None:
    root = copytree(PACKS, tmp_path / "packs")
    pack = root / "ecommerce_support"
    _, published = read_catalog(root)
    (pack / "README.md").write_text("# Updated demo instructions\n", encoding="utf-8")
    _, docs_edit = read_catalog(root)
    assert docs_edit[0].checksum == published[0].checksum

    (pack / "prompts" / "classify.v1.md").write_text("# Updated prompt\n", encoding="utf-8")
    _, prompt_edit = read_catalog(root)
    assert prompt_edit[0].checksum != published[0].checksum
