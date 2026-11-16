"""demo_live ownership, cleanup and timeout handling.

The unit tests need no Docker. The lifecycle test runs the public command and
needs the offline n8n mode (scripts/test_core.sh --offline-n8n).
"""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from runpy import run_path
from time import monotonic, sleep
from typing import Any

import pytest

from harness import demo_live, live_stack
from harness.live_stack import NORTHWIND, ROOT, LiveStack, SlackFixture, StackError

CONTAINER = "switchboard-core-check-" + "a" * 24
VOLUME = "switchboard-n8n-test-" + "b" * 32


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SWITCHBOARD_DEMO_LIVE_STATE_DIR", str(tmp_path))
    return tmp_path


def stack(tmp_path: Path) -> LiveStack:
    (tmp_path / "workflows").mkdir(exist_ok=True)
    return LiveStack(
        config=None,  # type: ignore[arg-type]
        credentials=None,  # type: ignore[arg-type]
        core="http://127.0.0.1:1",
        checkpoint_url="http://127.0.0.1:2/checkpoint",
        history=tmp_path / "audit.jsonl",
        slack=SlackFixture("x" * 64),
        volume=VOLUME,
        imports=tmp_path,
        _n8n_env={"N8N_ENCRYPTION_KEY": "not-shown"},
    )


def test_n8n_timeout_becomes_a_sanitized_stack_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "grant-0123456789abcdef"

    def hang(command: list[str], **_: Any) -> None:
        raise subprocess.TimeoutExpired(command, 100, output=secret, stderr=secret)

    monkeypatch.setattr(live_stack.subprocess, "run", hang)
    with pytest.raises(StackError) as error:
        stack(tmp_path).n8n("execute", "--id=SwitchboardDispatch1")
    message = str(error.value)
    assert message == "n8n execute --id=SwitchboardDispatch1 timed out after 100 seconds"
    assert secret not in message and "not-shown" not in message
    # No chained exception: the original carries the captured output.
    assert error.value.__cause__ is None and error.value.__suppress_context__


def test_docker_timeout_becomes_a_stack_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def hang(command: list[str], **_: Any) -> None:
        raise subprocess.TimeoutExpired(command, 80)

    monkeypatch.setattr(live_stack.subprocess, "run", hang)
    with pytest.raises(StackError, match="docker volume timed out"):
        live_stack.docker("volume", "create", VOLUME)


def test_claim_records_every_name_before_anything_exists(
    state_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert demo_live.claim() == 0
    state = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
    assert capsys.readouterr().out.strip() == state["container"]
    assert state["phase"] == "starting"
    container, volume, workdir = demo_live.owned_names(state)
    assert container and volume and workdir and not workdir.exists()


def test_second_claim_is_refused_and_changes_nothing(
    state_dir: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_docker(*_: Any, **__: Any) -> None:
        raise AssertionError("a refused claim must not run any command")

    assert demo_live.claim() == 0
    first = (state_dir / "state.json").read_bytes()
    capsys.readouterr()
    monkeypatch.setattr(demo_live.subprocess, "run", no_docker)
    assert demo_live.claim() == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "already running" in captured.err
    assert (state_dir / "state.json").read_bytes() == first


def test_abandoned_claim_is_reported_as_stale(
    state_dir: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert demo_live.claim() == 0
    monkeypatch.setattr(demo_live, "STARTUP_GRACE_SECONDS", 0)
    assert demo_live.claim() == 1
    assert "did not shut down cleanly" in capsys.readouterr().err
    assert demo_live.status() == 0
    assert capsys.readouterr().out.startswith("stale")


def test_up_refuses_to_run_without_its_own_claim(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SWITCHBOARD_CORE_TEST_CONTAINER", CONTAINER)
    assert demo_live.up(run_smoke=False) == 1  # no claim at all
    assert demo_live.claim() == 0  # a claim for a different container
    assert demo_live.up(run_smoke=False) == 1


class FakeDocker:
    """Answers the listing and removal commands demo_live uses."""

    def __init__(self, existing: set[str], *, stuck: set[str] = frozenset(), broken: bool = False):
        self.existing, self.stuck, self.broken = set(existing), set(stuck), broken
        self.removals: list[str] = []

    def __call__(self, command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        args = command[3:]  # after: docker --context desktop-linux
        if self.broken:
            return subprocess.CompletedProcess(command, 1, "", "daemon unavailable")
        kinds = {"ps": "container", "network": "network", "volume": "volume"}
        if args[0] == "ps" or args[:2] in (["network", "ls"], ["volume", "ls"]):
            wanted = args[-1].removeprefix("name=")
            names = [
                name.split(" ", 1)[1]
                for name in sorted(self.existing)
                if name.startswith(kinds[args[0]]) and wanted in name
            ]
            return subprocess.CompletedProcess(command, 0, "\n".join(names), "")
        if args[0] == "compose":
            targets = {n for n in self.existing if "-relay" in n or "_offline" in n}
        elif args[0] == "rm":
            targets = {f"container {args[-1]}"}
        else:
            targets = {f"volume {args[-1]}"}
        self.removals.append(args[0])
        failed = bool(targets & self.stuck)
        self.existing -= targets - self.stuck
        return subprocess.CompletedProcess(command, 1 if failed else 0, "", "")


def leftover_state(state_dir: Path, monkeypatch: pytest.MonkeyPatch) -> set[str]:
    assert demo_live.claim() == 0
    state = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
    monkeypatch.setattr(demo_live, "STARTUP_GRACE_SECONDS", 0)  # the owner is gone
    container, volume = state["container"], state["volume"]
    return {
        f"container {container}",
        f"container {container}-offline-relay-1",
        f"network {container}_offline-ai",
        f"volume {volume}",
    }


def test_down_removes_leftovers_then_the_state_and_is_safe_twice(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker = FakeDocker(leftover_state(state_dir, monkeypatch))
    monkeypatch.setattr(demo_live.subprocess, "run", docker)
    assert demo_live.down() == 0
    assert docker.existing == set() and not (state_dir / "state.json").exists()
    assert docker.removals == ["compose", "rm", "volume"]
    assert demo_live.down() == 0  # nothing left to do
    assert docker.removals == ["compose", "rm", "volume"]


def test_down_keeps_the_state_and_fails_when_something_remains(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    existing = leftover_state(state_dir, monkeypatch)
    volume = next(name for name in existing if name.startswith("volume"))
    docker = FakeDocker(existing, stuck={volume})
    monkeypatch.setattr(demo_live.subprocess, "run", docker)
    assert demo_live.down() == 1
    assert (state_dir / "state.json").exists()
    assert volume in capsys.readouterr().err
    # Once the obstacle is gone, the same command finishes the job.
    docker.stuck = set()
    assert demo_live.down() == 0
    assert not (state_dir / "state.json").exists()


def test_down_keeps_the_state_when_docker_cannot_be_asked(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    docker = FakeDocker(leftover_state(state_dir, monkeypatch), broken=True)
    monkeypatch.setattr(demo_live.subprocess, "run", docker)
    assert demo_live.down() == 1
    assert (state_dir / "state.json").exists()
    assert "cannot be checked" in capsys.readouterr().err


def test_state_names_that_are_not_ours_are_never_removed(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (state_dir / "state.json").write_text(
        json.dumps(
            {
                "phase": "running",
                "heartbeat": 0,
                "container": "postgres-production",
                "volume": "customer-data",
                "workdir": str(Path.home()),
            }
        ),
        encoding="utf-8",
    )
    docker = FakeDocker({"container postgres-production", "volume customer-data"})
    monkeypatch.setattr(demo_live.subprocess, "run", docker)
    assert demo_live.down() == 0
    assert docker.removals == [] and len(docker.existing) == 2
    assert Path.home().exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows readers can deny delete sharing")
def test_heartbeat_survives_an_open_state_reader(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = {"phase": "running", "heartbeat": 1}
    demo_live.write_state(state)
    path = state_dir / "state.json"
    with path.open(encoding="utf-8") as reader:

        def release_reader(delay: float) -> None:
            if delay == demo_live.HEARTBEAT_SECONDS:
                demo_live.stop_file().touch()
            else:
                # The ordinary reader holds the real Windows file handle until
                # the first replacement fails. The old snapshot is still valid.
                assert json.load(reader) == state
                reader.close()

        monkeypatch.setattr(demo_live, "sleep", release_reader)
        demo_live.hold(state)

    assert json.loads(path.read_text(encoding="utf-8"))["heartbeat"] > state["heartbeat"]
    assert not path.with_suffix(".tmp").exists()


def test_state_write_surfaces_unrelated_permission_errors(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def denied(*_: object) -> None:
        raise PermissionError("read-only directory")

    def unexpected_sleep(_: float) -> None:
        pytest.fail("unrelated permission errors must fail immediately")

    monkeypatch.setattr(Path, "replace", denied)
    monkeypatch.setattr(demo_live, "sleep", unexpected_sleep)
    with pytest.raises(PermissionError, match="read-only directory"):
        demo_live.write_state({"phase": "running", "heartbeat": 1})
    assert list(state_dir.iterdir()) == []


def run_demo_live(
    env: dict[str, str], *args: str, timeout: int = 240
) -> subprocess.CompletedProcess[str]:
    bash = shutil.which("bash")
    assert bash is not None
    return subprocess.run(
        [bash, "scripts/demo_live", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def docker_names(*args: str) -> list[str]:
    result = subprocess.run(
        [*live_stack.DOCKER, *args], capture_output=True, text=True, check=True, timeout=60
    )
    return result.stdout.split()


def test_public_lifecycle_up_smoke_status_second_up_down(tmp_path: Path) -> None:
    if os.environ.get("SWITCHBOARD_TEST_N8N") != "1":
        pytest.skip("run scripts/test_core.sh --offline-n8n")
    bash = shutil.which("bash")
    assert bash is not None
    env = {**os.environ, "SWITCHBOARD_DEMO_LIVE_STATE_DIR": str(tmp_path)}
    state_path, log_path = tmp_path / "state.json", tmp_path / "up.log"
    container = volume = ""
    with log_path.open("w", encoding="utf-8") as log:
        owner = subprocess.Popen(
            [bash, "scripts/demo_live", "up", "--smoke"],
            cwd=ROOT,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            until = monotonic() + 420
            while "Live demo stack ready" not in log_path.read_text(encoding="utf-8"):
                assert owner.poll() is None, log_path.read_text(encoding="utf-8")[-3000:]
                assert monotonic() < until, "the stack did not become ready"
                sleep(2)
            output = log_path.read_text(encoding="utf-8")
            assert "Smoke: NW-1002 | ORDER_STATUS: auto | draft sent" in output
            state = json.loads(state_path.read_text(encoding="utf-8"))
            container, volume = state["container"], state["volume"]
            assert state["phase"] == "running"

            status = run_demo_live(env, "status")
            assert status.returncode == 0 and status.stdout.startswith(
                "running | core http://127.0.0.1:"
            )

            # The smoke case as the reporting login sees it in that stack's own database.
            case = re.search(r"^Smoke case: ([0-9a-f-]{36})$", output, re.MULTILINE)
            assert case is not None
            marker = docker_names("exec", container, "printenv", "SWITCHBOARD_CORE_TEST_MARKER")[0]
            report = run_path(str(ROOT / "scripts" / "demo_report"))
            rows = report["read_timeline"](container, marker, NORTHWIND, {case[1]})["rows"]
            assert {row["case_id"] for row in rows} == {case[1]}
            assert any(
                (row["event_kind"], row["rule_id"], row["decision"])
                == ("policy_decision", "ORDER_STATUS", "auto")
                for row in rows
            )
            assert any((row["event_kind"], row["status"]) == ("draft", "sent") for row in rows)

            # A second start is refused and leaves the first one untouched and working.
            before = docker_names("ps", "--all", "--format", "{{.Names}}")
            second = run_demo_live(env, "up")
            assert second.returncode == 1 and "already running" in second.stderr
            assert docker_names("ps", "--all", "--format", "{{.Names}}") == before
            assert owner.poll() is None
            beat = json.loads(state_path.read_text(encoding="utf-8"))["heartbeat"]
            sleep(2 * demo_live.HEARTBEAT_SECONDS + 1)
            assert json.loads(state_path.read_text(encoding="utf-8"))["heartbeat"] > beat, (
                f"owner exit={owner.poll()}\n" + log_path.read_text(encoding="utf-8")[-3000:]
            )
            again = report["read_timeline"](container, marker, NORTHWIND, {case[1]})["rows"]
            assert len(again) == len(rows)
        finally:
            stopped = run_demo_live(env, "down")
            try:
                owner.wait(timeout=120)
            except subprocess.TimeoutExpired:
                owner.kill()
                raise
    assert stopped.returncode == 0, stopped.stdout + stopped.stderr
    assert owner.returncode == 0, log_path.read_text(encoding="utf-8")[-3000:]
    assert not state_path.exists()
    assert container and volume
    assert (
        docker_names("ps", "--all", "--format", "{{.Names}}", "--filter", f"name={container}") == []
    )
    assert (
        docker_names("network", "ls", "--format", "{{.Name}}", "--filter", f"name={container}")
        == []
    )
    assert docker_names("volume", "ls", "--quiet", "--filter", f"name={volume}") == []
    assert not Path(state["workdir"]).exists()
    # Stopping again is harmless.
    assert run_demo_live(env, "down").returncode == 0
