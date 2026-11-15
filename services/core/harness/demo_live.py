"""Hold the live demo stack up until asked to stop (ADR 0030).

Run through ``scripts/demo_live``, which creates the disposable database and
offline gateway first and removes them when this process exits.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from shutil import rmtree
from tempfile import TemporaryDirectory, gettempdir
from time import monotonic, sleep, time

from app.adapters.db import Settings, create_pool
from harness.live_stack import DOCKER, NORTHWIND, ROOT, LiveStack, launch, set_northwind

STATE_DIR = ROOT / ".demo-live"
STATE = STATE_DIR / "state.json"
STOP = STATE_DIR / "stop"
HEARTBEAT_SECONDS = 2
STALE_AFTER_SECONDS = 20
INGRESS_TTL_MINUTES = 12 * 60
WORKDIR_PREFIX = "switchboard-live-"
# Names from the state file are data. Only remove resources this harness names.
CONTAINER_NAME = re.compile(r"switchboard-core-check-[0-9a-f]{24}")
VOLUME_NAME = re.compile(r"switchboard-n8n-test-[0-9a-f]{32}")


def read_state() -> dict[str, object] | None:
    try:
        state = json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return state if isinstance(state, dict) else None


def is_fresh(state: dict[str, object]) -> bool:
    beat = state.get("heartbeat")
    return isinstance(beat, int | float) and time() - beat < STALE_AFTER_SECONDS


def write_state(stack: LiveStack, workdir: str) -> None:
    # Names and a heartbeat only. Tokens and passwords never reach this file.
    state = {
        "container": os.environ["SWITCHBOARD_CORE_TEST_CONTAINER"],
        "volume": stack.volume,
        "core": stack.core,
        "workdir": workdir,
        "heartbeat": time(),
    }
    temporary = STATE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state), encoding="utf-8")
    temporary.replace(STATE)


def smoke(stack: LiveStack) -> None:
    """Send one new order-status message through the stack and print what happened."""
    external_id = stack.send_email("liam.chen@example.com", "Where is my order NW-1002?")
    stack.n8n("execute", "--id=SwitchboardDispatch1")
    with create_pool(stack.config) as pool, pool.connection() as conn, conn.transaction():
        set_northwind(conn)
        rows = conn.execute(
            """SELECT p.decision,p.rule_id,d.status,d.body FROM public.messages m
               JOIN public.drafts d ON (d.tenant_id,d.in_reply_to_message_id)=(m.tenant_id,m.id)
               JOIN public.policy_decisions p
                 ON (p.tenant_id,p.id)=(d.tenant_id,d.policy_decision_id)
               WHERE m.tenant_id=%s AND m.external_message_id=%s""",
            (NORTHWIND, external_id),
        ).fetchall()
    for decision, rule_id, status, body in rows:
        print(f"Smoke: NW-1002 | {rule_id}: {decision} | draft {status}\n{body}", flush=True)
    if [row[:3] for row in rows] != [("auto", "ORDER_STATUS", "sent")]:
        raise SystemExit("Smoke message did not produce one automatic sent reply")


def check() -> int:
    state = read_state()
    if state is None:
        return 0
    if is_fresh(state):
        print("The live demo stack is already running. Stop it with: scripts/demo_live down")
    else:
        print("A previous live demo stack did not shut down cleanly. Run: scripts/demo_live down")
    return 1


def up(run_smoke: bool) -> int:
    STATE_DIR.mkdir(exist_ok=True)
    STOP.unlink(missing_ok=True)
    base = Settings.from_environment()
    print("Starting core, the checkpoint process and scratch n8n (about a minute)", flush=True)
    try:
        with (
            TemporaryDirectory(prefix=WORKDIR_PREFIX) as workdir,
            launch(base, Path(workdir), ingress_ttl_minutes=INGRESS_TTL_MINUTES) as stack,
        ):
            write_state(stack, workdir)
            if run_smoke:
                smoke(stack)
            print(
                f"Live demo stack ready. Core listens on {stack.core} (loopback only).\n"
                "Mock adapters and the fake model only. Stop with Ctrl+C or: scripts/demo_live down",
                flush=True,
            )
            while not STOP.exists():
                write_state(stack, workdir)
                sleep(HEARTBEAT_SECONDS)
            print("Stop requested. Shutting down.", flush=True)
    except KeyboardInterrupt:
        print("Interrupted. Shutting down.", flush=True)
    finally:
        STATE.unlink(missing_ok=True)
        STOP.unlink(missing_ok=True)
    return 0


def force_remove(state: dict[str, object]) -> None:
    """Remove what a crashed ``up`` left behind, by the exact names it recorded."""
    container, volume = state.get("container"), state.get("volume")
    if isinstance(container, str) and CONTAINER_NAME.fullmatch(container):
        subprocess.run(
            [*DOCKER, "compose", "--env-file", ".env.example", "-p", container]
            + ["--profile", "offline-ai", "down"],
            cwd=ROOT,
            capture_output=True,
            check=False,
            timeout=120,
        )
        subprocess.run(
            [*DOCKER, "rm", "--force", "--volumes", container],
            capture_output=True,
            check=False,
            timeout=60,
        )
    if isinstance(volume, str) and VOLUME_NAME.fullmatch(volume):
        subprocess.run(
            [*DOCKER, "volume", "rm", volume], capture_output=True, check=False, timeout=60
        )
    workdir = state.get("workdir")
    if isinstance(workdir, str):
        path = Path(workdir)
        if path.parent == Path(gettempdir()) and path.name.startswith(WORKDIR_PREFIX):
            rmtree(path, ignore_errors=True)


def container_exists(name: object) -> bool:
    if not isinstance(name, str) or not CONTAINER_NAME.fullmatch(name):
        return False
    result = subprocess.run(
        [*DOCKER, "ps", "--all", "--quiet", "--filter", f"name=^{name}$"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    return bool(result.stdout.strip())


def down() -> int:
    state = read_state()
    if state is None:
        STOP.unlink(missing_ok=True)
        print("The live demo stack is not running.")
        return 0
    if is_fresh(state):
        STOP.write_text("", encoding="utf-8")
        until = monotonic() + 120
        while STATE.exists() and monotonic() < until:
            sleep(0.5)
        # The wrapper script removes the database and gateway after the process exits.
        while container_exists(state.get("container")) and monotonic() < until:
            sleep(0.5)
    if STATE.exists() or container_exists(state.get("container")):
        print("Removing leftovers from a stack that did not shut down by itself.")
        force_remove(state)
    STATE.unlink(missing_ok=True)
    STOP.unlink(missing_ok=True)
    print("The live demo stack is stopped.")
    return 0


def status() -> int:
    state = read_state()
    if state is None:
        print("not running")
    elif is_fresh(state):
        print(f"running | core {state.get('core')} | database container {state.get('container')}")
    else:
        print("stale: the stack process is gone. Run: scripts/demo_live down")
    return 0


def main(arguments: list[str]) -> int:
    match arguments:
        case ["check"]:
            return check()
        case ["up"]:
            return up(run_smoke=False)
        case ["up", "--smoke"]:
            return up(run_smoke=True)
        case ["down"]:
            return down()
        case ["status"]:
            return status()
    print("Usage: scripts/demo_live up [--smoke] | down | status", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
