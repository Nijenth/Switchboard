from collections.abc import Callable
from pathlib import Path
from runpy import run_path
from typing import cast

import pytest


def test_checkpoint_file_compares_before_appending(tmp_path: Path) -> None:
    script = Path(__file__).resolve().parents[3] / "scripts" / "checkpoint_audit.py"
    command = run_path(str(script))
    advance = cast(Callable[[Path, str, str, bool], int], command["advance"])
    history = tmp_path / "audit.jsonl"
    expected: dict[str, int | str] = {"chain_seq": 0, "row_hash": "GENESIS"}
    head: dict[str, int | str] = {"chain_seq": 1, "row_hash": "a" * 64}

    def verify(url: str, token: str, checkpoint: dict[str, int | str]) -> dict[str, int | str]:
        assert checkpoint == expected
        return head

    command["advance"].__globals__["verify"] = verify
    with pytest.raises(FileNotFoundError):
        advance(history, "unused", "unused", False)
    assert advance(history, "unused", "unused", True) == 1
    expected = head
    head = {"chain_seq": 2, "row_hash": "b" * 64}
    assert advance(history, "unused", "unused", False) == 2
    before = history.read_bytes()
    expected = head
    head = {"chain_seq": 2, "row_hash": "c" * 64}
    with pytest.raises(ValueError, match="hash changed"):
        advance(history, "unused", "unused", False)
    assert history.read_bytes() == before
    assert not history.with_suffix(".jsonl.lock").exists()
