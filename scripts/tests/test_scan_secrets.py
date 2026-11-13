from pathlib import Path

from pytest import CaptureFixture, MonkeyPatch

from scripts import scan_secrets


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
