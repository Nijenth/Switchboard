import json
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from types import ModuleType

import pytest
from pytest import CaptureFixture, MonkeyPatch

from scripts import scan_secrets


def load_exporter() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "export_workflows"
    spec = spec_from_loader("export_workflows", SourceFileLoader("export_workflows", str(path)))
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_scanner_reports_location_without_credential(
    tmp_path: Path, monkeypatch: MonkeyPatch, capsys: CaptureFixture[str]
) -> None:
    credential = "gh" + "p_" + "A" * 36
    (tmp_path / "fixture.txt").write_text(f"line one\n{credential}\n", encoding="utf-8")
    monkeypatch.setattr(scan_secrets, "__file__", str(tmp_path / "scripts" / "scan_secrets.py"))
    monkeypatch.setattr(scan_secrets.subprocess, "check_output", lambda *a, **kw: b"fixture.txt\0")

    assert scan_secrets.main() == 1
    output = capsys.readouterr().out
    assert "fixture.txt:2: possible GitHub token" in output
    assert credential not in output


@pytest.mark.parametrize(
    "snippet",
    [
        "https://user:hunter2@core.internal/lookups",
        "https://core.internal/lookups?api_key=supersecret",
        "https://core.internal/lookups#token=supersecret",
        "const apiKey = 'sk-proj-" + "a" * 36 + "';",
        "const secret = 'my-inline-secret';",
        "const auth = 'Bearer topsecret';",
        "return [{json: {key: process.env.SECRET_KEY}}];",
        "const hex = '" + "a1" * 32 + "';",
    ],
)
def test_export_workflows_refuses_secrets_in_urls_and_code_nodes(snippet: str) -> None:
    exporter = load_exporter()
    root = Path(__file__).resolve().parents[2]
    workflows = [
        json.loads(file.read_text(encoding="utf-8"))
        for file in sorted((root / "workflows").glob("*.json"))
    ]
    assert len(exporter.export(json.dumps(workflows))) == len(workflows)

    mutated = json.loads(json.dumps(workflows))
    code_node = next(
        node for wf in mutated for node in wf["nodes"] if node["type"] == "n8n-nodes-base.code"
    )
    code_node["parameters"]["jsCode"] = snippet
    with pytest.raises(ValueError, match="inline secret"):
        exporter.export(json.dumps(mutated))
