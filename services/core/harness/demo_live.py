"""Hold the live demo stack up until asked to stop (ADR 0030).

Run through ``scripts/demo_live``. The order matters:

1. ``claim`` takes ownership by creating the state file exclusively and records
   the name of every resource this run will create, before anything exists.
2. The wrapper creates the disposable database and offline gateway.
3. ``up`` builds the rest of the stack under the recorded names and holds it.
4. The wrapper removes the database and gateway, then ``release`` removes what
   is left, checks that nothing remains, and only then deletes the state file.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from secrets import token_hex
from shutil import rmtree
from tempfile import gettempdir
from time import monotonic, sleep, time
from uuid import uuid4

from app.adapters.db import Settings, create_pool
from harness.console import console_for_stack
from harness.live_stack import (
    DOCKER,
    NORTHWIND,
    ROOT,
    LiveStack,
    StackError,
    launch,
    serve,
    set_northwind,
)

HEARTBEAT_SECONDS = 2
STALE_AFTER_SECONDS = 20
# How long a claimed stack may take to start before it counts as abandoned.
STARTUP_GRACE_SECONDS = 300
INGRESS_TTL_MINUTES = 12 * 60
CONSOLE_PORT = 8780
WORKDIR_PREFIX = "switchboard-live-"
# Names from the state file are data. Only remove resources this harness names.
CONTAINER_NAME = re.compile(r"switchboard-core-check-[0-9a-f]{24}")
VOLUME_NAME = re.compile(r"switchboard-n8n-test-[0-9a-f]{32}")
WORKDIR_NAME = re.compile(WORKDIR_PREFIX + r"[0-9a-f]{16}")

State = dict[str, object]


def state_dir() -> Path:
    """The folder that holds the state file. Tests point it at a scratch folder."""
    override = os.environ.get("SWITCHBOARD_DEMO_LIVE_STATE_DIR")
    if not override:
        return ROOT / ".demo-live"
    path = Path(override)
    if not path.is_absolute():
        raise SystemExit("SWITCHBOARD_DEMO_LIVE_STATE_DIR must be an absolute path")
    return path


def state_file() -> Path:
    return state_dir() / "state.json"


def stop_file() -> Path:
    return state_dir() / "stop"


def read_state() -> State | None:
    try:
        state = json.loads(state_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return state if isinstance(state, dict) else None


def is_alive(state: State) -> bool:
    """A starting stack is alive during its grace period; a running one while it heartbeats."""
    if state.get("phase") == "starting":
        claimed = state.get("claimed")
        return isinstance(claimed, int | float) and time() - claimed < STARTUP_GRACE_SECONDS
    beat = state.get("heartbeat")
    return isinstance(beat, int | float) and time() - beat < STALE_AFTER_SECONDS


def write_state(state: State) -> None:
    # Names and timestamps only. Tokens and passwords never reach this file.
    temporary = state_file().with_suffix(".tmp")
    temporary.write_text(json.dumps(state), encoding="utf-8")
    temporary.replace(state_file())


def owned_names(state: State) -> tuple[str | None, str | None, Path | None]:
    """The container, volume and work folder this state may touch, if their names are ours."""
    container, volume, workdir = state.get("container"), state.get("volume"), state.get("workdir")
    folder = None
    if isinstance(workdir, str):
        path = Path(workdir)
        if path.parent == Path(gettempdir()) and WORKDIR_NAME.fullmatch(path.name):
            folder = path
    return (
        container if isinstance(container, str) and CONTAINER_NAME.fullmatch(container) else None,
        volume if isinstance(volume, str) and VOLUME_NAME.fullmatch(volume) else None,
        folder,
    )


def claim() -> int:
    """Take ownership atomically and record every resource name before anything is created."""
    state_dir().mkdir(parents=True, exist_ok=True)
    state: State = {
        "phase": "starting",
        "claimed": time(),
        "container": "switchboard-core-check-" + token_hex(12),
        "volume": "switchboard-n8n-test-" + uuid4().hex,
        "workdir": str(Path(gettempdir()) / (WORKDIR_PREFIX + token_hex(8))),
    }
    try:
        # O_EXCL: of two concurrent starts exactly one creates the file.
        descriptor = os.open(state_file(), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        existing = read_state()
        if existing is not None and is_alive(existing):
            message = "The live demo stack is already running. Stop it with: scripts/demo_live down"
        else:
            message = (
                "A previous live demo stack did not shut down cleanly. Run: scripts/demo_live down"
            )
        print(message, file=sys.stderr)
        return 1
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(state, handle)
    stop_file().unlink(missing_ok=True)
    # The wrapper reads the database container name from stdout.
    print(state["container"])
    return 0


def smoke(stack: LiveStack) -> None:
    """Send one new order-status message through the stack and print what happened."""
    external_id = stack.send_email("liam.chen@example.com", "Where is my order NW-1002?")
    stack.n8n("execute", "--id=SwitchboardDispatch1")
    with create_pool(stack.config) as pool, pool.connection() as conn, conn.transaction():
        set_northwind(conn)
        rows = conn.execute(
            """SELECT p.decision,p.rule_id,d.status,d.body,m.case_id::text FROM public.messages m
               JOIN public.drafts d ON (d.tenant_id,d.in_reply_to_message_id)=(m.tenant_id,m.id)
               JOIN public.policy_decisions p
                 ON (p.tenant_id,p.id)=(d.tenant_id,d.policy_decision_id)
               WHERE m.tenant_id=%s AND m.external_message_id=%s""",
            (NORTHWIND, external_id),
        ).fetchall()
    for decision, rule_id, status, body, case_id in rows:
        print(f"Smoke: NW-1002 | {rule_id}: {decision} | draft {status}\n{body}", flush=True)
        print(f"Smoke case: {case_id}", flush=True)
    if [row[:3] for row in rows] != [("auto", "ORDER_STATUS", "sent")]:
        raise SystemExit("Smoke message did not produce one automatic sent reply")


def hold(state: State) -> None:
    while not stop_file().exists():
        write_state({**state, "heartbeat": time()})
        sleep(HEARTBEAT_SECONDS)
    print("Stop requested. Shutting down.", flush=True)


def up(run_smoke: bool, run_console: bool = False) -> int:
    state = read_state()
    container, volume, workdir = owned_names(state or {})
    if (
        state is None
        or state.get("phase") != "starting"
        or container != os.environ.get("SWITCHBOARD_CORE_TEST_CONTAINER")
        or volume is None
        or workdir is None
    ):
        print("Start the stack with scripts/demo_live up, which claims it first.", file=sys.stderr)
        return 1
    base = Settings.from_environment()
    print("Starting core, the checkpoint process and scratch n8n (about a minute)", flush=True)
    try:
        workdir.mkdir()
        with launch(base, workdir, ingress_ttl_minutes=INGRESS_TTL_MINUTES, volume=volume) as stack:
            state = {**state, "phase": "running", "core": stack.core, "heartbeat": time()}
            write_state(state)
            if run_smoke:
                smoke(stack)
            print(
                f"Live demo stack ready. Core listens on {stack.core} (loopback only).\n"
                "Mock adapters and the fake model only. Stop with Ctrl+C or: scripts/demo_live down",
                flush=True,
            )
            if run_console:
                console = console_for_stack(
                    stack,
                    os.environ["SWITCHBOARD_CORE_TEST_CONTAINER"],
                    os.environ["SWITCHBOARD_CORE_TEST_MARKER"],
                )
                with serve(console, CONSOLE_PORT) as url:
                    print(f"Demo console: open {url} in a browser on this machine.", flush=True)
                    hold(state)
            else:
                hold(state)
    except KeyboardInterrupt:
        print("Interrupted. Shutting down.", flush=True)
    finally:
        rmtree(workdir, ignore_errors=True)
    # The state file stays until ``release`` has confirmed that nothing remains.
    return 0


def docker_lines(*args: str) -> list[str]:
    """Run a docker listing command. A failed listing is an error, never 'nothing found'."""
    try:
        result = subprocess.run(
            [*DOCKER, *args], capture_output=True, text=True, check=False, timeout=60
        )
    except subprocess.TimeoutExpired:
        raise StackError(f"docker {args[0]} timed out") from None
    if result.returncode != 0:
        raise StackError(f"docker {args[0]} failed, so leftovers cannot be checked")
    return [line for line in result.stdout.splitlines() if line.strip()]


def remaining(state: State) -> list[str]:
    """Everything this state names that still exists."""
    container, volume, workdir = owned_names(state)
    left = []
    if container:
        # The name filter also matches the gateway project's containers and networks.
        names = docker_lines(
            "ps", "--all", "--format", "{{.Names}}", "--filter", f"name={container}"
        )
        left += [f"container {name}" for name in names]
        networks = docker_lines(
            "network", "ls", "--format", "{{.Name}}", "--filter", f"name={container}"
        )
        left += [f"network {name}" for name in networks]
    if volume:
        left += [
            f"volume {name}"
            for name in docker_lines("volume", "ls", "--quiet", "--filter", f"name={volume}")
        ]
    if workdir and workdir.exists():
        left.append(f"folder {workdir}")
    return left


def remove_leftovers(state: State) -> list[str]:
    """Remove what the state names. Returns what could not be removed."""
    container, volume, workdir = owned_names(state)
    failed = []

    def run(label: str, *args: str, cwd: Path | None = None) -> None:
        try:
            result = subprocess.run(
                [*DOCKER, *args], cwd=cwd, capture_output=True, check=False, timeout=120
            )
        except subprocess.TimeoutExpired:
            failed.append(f"{label}: docker timed out")
            return
        if result.returncode != 0:
            failed.append(f"{label}: docker exited {result.returncode}")

    left = remaining(state)
    if container and any(item.startswith(("container", "network")) for item in left):
        run(
            "gateway project",
            *("compose", "--env-file", ".env.example", "-p", container),
            *("--profile", "offline-ai", "down"),
            cwd=ROOT,
        )
        if f"container {container}" in left:
            run(f"container {container}", "rm", "--force", "--volumes", container)
    if volume and f"volume {volume}" in left:
        run(f"volume {volume}", "volume", "rm", volume)
    if workdir:
        rmtree(workdir, ignore_errors=True)
    # What still exists is the authority; a failed command with nothing left is not a failure.
    still = remaining(state)
    return still + [item for item in failed if still]


def finish(state: State) -> int:
    """Remove leftovers and delete the state file only when nothing remains."""
    try:
        left = remove_leftovers(state)
    except StackError as error:
        print(f"Could not finish cleaning up: {error}", file=sys.stderr)
        print("The state file is kept. Fix Docker and run: scripts/demo_live down", file=sys.stderr)
        return 1
    if left:
        print("These resources could not be removed:", file=sys.stderr)
        for item in left:
            print(f"  {item}", file=sys.stderr)
        print("The state file is kept. Run: scripts/demo_live down", file=sys.stderr)
        return 1
    state_file().unlink(missing_ok=True)
    stop_file().unlink(missing_ok=True)
    return 0


def release() -> int:
    """Called by the wrapper after it removed the database and gateway."""
    state = read_state()
    return 0 if state is None else finish(state)


def down() -> int:
    state = read_state()
    if state is None:
        stop_file().unlink(missing_ok=True)
        print("The live demo stack is not running.")
        return 0
    if is_alive(state):
        stop_file().write_text("", encoding="utf-8")
        # The owner process stops, its wrapper cleans up and releases the state file.
        until = monotonic() + 180
        while state_file().exists() and monotonic() < until:
            sleep(0.5)
        state = read_state()
        if state is None:
            print("The live demo stack is stopped.")
            return 0
        print("The stack did not stop by itself. Removing what it left.")
    else:
        print("Removing leftovers from a stack that did not shut down by itself.")
    status = finish(state)
    if status == 0:
        print("The live demo stack is stopped.")
    return status


def status() -> int:
    state = read_state()
    if state is None:
        print("not running")
    elif not is_alive(state):
        print("stale: the stack process is gone. Run: scripts/demo_live down")
    elif state.get("phase") == "starting":
        print(f"starting | database container {state.get('container')}")
    else:
        print(f"running | core {state.get('core')} | database container {state.get('container')}")
    return 0


def main(arguments: list[str]) -> int:
    match arguments:
        case ["claim"]:
            return claim()
        case ["up", *flags] if set(flags) <= {"--smoke", "--console"}:
            return up(run_smoke="--smoke" in flags, run_console="--console" in flags)
        case ["release"]:
            return release()
        case ["down"]:
            return down()
        case ["status"]:
            return status()
    print("Usage: scripts/demo_live up [--smoke] [--console] | down | status", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
