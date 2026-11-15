"""Assemble the mock stack: core in-process, one-shot n8n CLI runs, local fixtures.

Everything here targets the disposable integration database created by
``scripts/core_stack.sh``. Each login checks the per-run cluster marker first.
"""

import json
import os
import re
import socket
import subprocess
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from hashlib import sha256
from hmac import digest
from pathlib import Path
from secrets import token_hex
from threading import Thread
from time import monotonic, sleep, time
from typing import Any
from urllib.parse import urlencode
from uuid import UUID, uuid4

import httpx
import psycopg
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from psycopg import Connection

from app.adapters.db import Settings
from app.main import create_app

ROOT = Path(__file__).resolve().parents[3]
IMAGE = "docker.n8n.io/n8nio/n8n:2.46.7"
DOCKER = ("docker", "--context", "desktop-linux")
NORTHWIND = UUID("10000000-0000-0000-0000-000000000001")
SUPPORT = "support@northwind.example"
WORKSPACE, REVIEWER, CHANNEL, MESSAGE_TS = "TWORKFLOW", "UWORKFLOW", "CWORKFLOW", "11.123456"
ADMIN = "20000000-0000-0000-0000-000000000004"
MANAGER = "20000000-0000-0000-0000-000000000002"
MANAGER_SLACK = "UDEMOMARCO"
# Sub-workflows must be published before a parent can call them.
SUB_WORKFLOWS = (
    "SwitchboardError1",
    "SwitchboardSend1",
    "SwitchboardRefundIssue1",
    "SwitchboardConfig1",
    "SwitchboardIdentity1",
    "SwitchboardOrder1",
    "SwitchboardRefundEligibility1",
    "SwitchboardLookups1",
    "SwitchboardCase1",
)
EXECUTION_ROLES = ("worker", "reconciler", "approval_poster")
N8N_TIMEOUT_SECONDS = 100
N8N_FAILURE_HINTS = (
    "paired item",
    "multiple matching items",
    "not been executed",
    "invalid syntax",
    "is not iterable",
    "cannot read properties",
    "can't determine which item",
    "only 1 item",
    "No data found",
    "doesn't exist",
    "referenced node",
    "no connection back",
    "trying to access item",
    "is not a function",
    "ambiguous",
)


class StackError(RuntimeError):
    """The mock stack could not be built or a step did not behave as required."""


@dataclass(frozen=True)
class EmailMessage:
    """One Gmail-shaped inbound message for the manual ingest fixture."""

    external_id: str
    sender: str
    text: str
    date: str = "2026-11-14T00:00:00Z"
    subject: str | None = None

    def item(self) -> dict[str, object]:
        message: dict[str, object] = {
            "id": self.external_id,
            "threadId": self.external_id,
            "from": {"value": [{"address": self.sender}]},
            "to": {"value": [{"address": SUPPORT}]},
            "text": self.text,
            "date": self.date,
        }
        if self.subject is not None:
            message["subject"] = self.subject
        return {"json": message}


@dataclass(frozen=True)
class StackCredentials:
    """Per-run tokens. They exist only in this process and the scratch n8n volume."""

    execution: dict[str, str] = field(repr=False)
    ingress: str = field(repr=False)
    understand: str = field(repr=False)
    retrieve: str = field(repr=False)
    draft: str = field(repr=False)
    decide: str = field(repr=False)
    lookup: str = field(repr=False)
    refund: str = field(repr=False)
    slack_bot: str = field(repr=False)
    schedule: str = field(repr=False)


def verify_cluster(connection: Connection[tuple[object, ...]], login: str) -> None:
    """Refuse any server that is not this run's disposable cluster, or any other login."""
    result = connection.execute(
        "SELECT current_setting('switchboard.integration_marker', true), session_user, current_user"
    ).fetchone()
    if result != (os.environ["SWITCHBOARD_CORE_TEST_MARKER"], login, login):
        raise StackError(f"Not the disposable integration cluster for login {login}")


def set_northwind(connection: Connection[tuple[object, ...]]) -> None:
    verify_cluster(connection, "switchboard_core")
    connection.execute("SELECT set_config('app.tenant_id', %s, true)", (str(NORTHWIND),))


def stack_settings(base: Settings) -> tuple[Settings, StackCredentials]:
    """Return core settings carrying fresh scoped credentials, plus those credentials."""
    credentials = StackCredentials(
        execution={role: token_hex(32) for role in EXECUTION_ROLES},
        ingress=token_hex(32),
        understand=token_hex(32),
        retrieve=token_hex(32),
        draft=token_hex(32),
        decide=token_hex(32),
        lookup=token_hex(32),
        refund=token_hex(32),
        slack_bot=token_hex(32),
        schedule=token_hex(32),
    )

    def scope(token: str) -> str:
        return json.dumps([{"tenant_id": str(NORTHWIND), "token": token}])

    config = replace(
        base,
        execution_credentials=json.dumps(
            [
                {"tenant_id": str(NORTHWIND), "role": role, "principal": role, "token": token}
                for role, token in credentials.execution.items()
            ]
        ),
        audit_token=token_hex(32),
        understand_credentials=scope(credentials.understand),
        retrieve_credentials=scope(credentials.retrieve),
        draft_credentials=scope(credentials.draft),
        decide_credentials=scope(credentials.decide),
        lookup_credentials=scope(credentials.lookup),
        refund_credentials=scope(credentials.refund),
    )
    return config, credentials


def prepare_tenant(
    config: Settings, credentials: StackCredentials, *, ingress_ttl_minutes: int = 30
) -> None:
    """Point the demo tenant at the local Slack fixture and register the ingress token.

    The ingress credential expires on the database clock after ``ingress_ttl_minutes``.
    """
    if not 1 <= ingress_ttl_minutes <= 24 * 60:
        raise StackError("Ingress credential lifetime must be between 1 minute and 24 hours")
    with psycopg.connect(
        host=config.db_host,
        port=config.db_port,
        dbname=config.db_name,
        user="switchboard_config_loader",
        password=os.environ["SWITCHBOARD_CONFIG_LOADER_PASSWORD"],
        connect_timeout=3,
    ) as loader:
        verify_cluster(loader, "switchboard_config_loader")
        loader.execute(
            "UPDATE public.tenant_settings SET slack_workspace_id=%s WHERE tenant_id=%s",
            (WORKSPACE, NORTHWIND),
        )
        for slack_user, staff in ((REVIEWER, ADMIN), (MANAGER_SLACK, MANAGER)):
            loader.execute(
                "UPDATE public.staff_users SET slack_user_id=%s WHERE tenant_id=%s AND id=%s",
                (slack_user, NORTHWIND, staff),
            )
        loader.execute(
            """UPDATE public.queues SET notify_channel=%s
               WHERE tenant_id=%s AND key IN ('accounts_payable','customer_support')""",
            (CHANNEL, NORTHWIND),
        )
        loader.execute(
            """INSERT INTO public.ingress_credentials (token_digest,tenant_id,endpoint_id,expires_at)
               SELECT %s,tenant_id,id,now()+make_interval(mins => %s) FROM public.channel_endpoints
               WHERE tenant_id=%s AND address=%s""",
            (
                sha256(credentials.ingress.encode()).digest(),
                ingress_ttl_minutes,
                NORTHWIND,
                SUPPORT,
            ),
        )


class SlackFixture:
    """Local stand-in for Slack's chat.postMessage. Records which reviews were posted."""

    def __init__(self, bot_token: str) -> None:
        self._bot_token = bot_token
        self.posted: set[str] = set()

    def install(self, app: FastAPI) -> None:
        # Added to this harness's app instance only. Core's own routes are unchanged.
        @app.post("/fixture/slack/chat.postMessage")
        async def fake_slack(request: Request) -> dict[str, object]:
            if request.headers.get("Authorization") != "Bearer " + self._bot_token:
                raise HTTPException(status_code=401)
            payload = await request.json()
            try:
                button = payload["blocks"][-1]["elements"][0]
                valid = (
                    payload["channel"] == CHANNEL and button["action_id"] == "switchboard_approve"
                )
            except (KeyError, IndexError, TypeError):
                valid = False
            if not valid:
                raise HTTPException(status_code=400)
            self.posted.add(str(button["value"]))
            return {"ok": True, "channel": CHANNEL, "ts": MESSAGE_TS}


@contextmanager
def serve(app: FastAPI, port: int = 0) -> Iterator[str]:
    """Serve on loopback only. Port 0 picks a free port."""
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            raise StackError(f"Port {port} on 127.0.0.1 is already in use") from None
        server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False))
        thread = Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        try:
            until = monotonic() + 10
            while not server.started and monotonic() < until:
                sleep(0.02)
            if not server.started:
                raise StackError("Core did not start listening")
            yield f"http://127.0.0.1:{sock.getsockname()[1]}"
        finally:
            server.should_exit = True
            thread.join(timeout=10)


@contextmanager
def checkpoint_process(
    history: Path, core: str, audit_token: str, schedule_token: str
) -> Iterator[str]:
    env = {
        **{key: value for key, value in os.environ.items() if not key.startswith("SWITCHBOARD_")},
        "SWITCHBOARD_CHECKPOINT_HISTORY": str(history),
        "SWITCHBOARD_AUDIT_URL": core + "/audit/verify",
        "SWITCHBOARD_AUDIT_TOKEN": audit_token,
        "SWITCHBOARD_CHECKPOINT_SCHEDULE_TOKEN": schedule_token,
    }
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "checkpoint_audit.py"),
            "--initialize",
            str(history),
        ],
        env=env,
        capture_output=True,
        check=True,
        timeout=15,
    )
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env["SWITCHBOARD_CHECKPOINT_PORT"] = str(port)
    # The wrapper exits when this process goes away, even if it is killed outright.
    child = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).with_name("checkpoint_child.py")),
            str(ROOT / "scripts" / "serve_audit_checkpoint.py"),
        ],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        until = monotonic() + 10
        while monotonic() < until:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                    break
            except OSError:
                if child.poll() is not None:
                    raise StackError("checkpoint fixture exited before listening") from None
                sleep(0.05)
        else:
            raise StackError("checkpoint fixture did not start")
        yield f"http://127.0.0.1:{port}/checkpoint"
    finally:
        child.terminate()
        child.wait(timeout=10)


def docker(*args: str) -> None:
    try:
        result = subprocess.run(
            [*DOCKER, *args], text=True, capture_output=True, timeout=80, check=False
        )
    except subprocess.TimeoutExpired:
        raise StackError(f"docker {args[0]} timed out after 80 seconds") from None
    if result.returncode != 0:
        raise StackError((result.stdout + result.stderr)[-4000:])


def _in_container(url: str) -> str:
    return url.replace("127.0.0.1", "host.docker.internal")


def _workflow_copy(source: Path, messages: Sequence[EmailMessage], *, replace_fixture: bool) -> str:
    """Return the import copy of one exported workflow.

    Schedules become manual triggers and mock database nodes get the scratch credential.
    The ingest fixture either gains ``messages`` or, for a live send, holds only them.
    """
    workflow = json.loads(source.read_text(encoding="utf-8"))
    for node in workflow["nodes"]:
        if node["type"] == "n8n-nodes-base.postgres":
            node["credentials"] = {"postgres": {"id": "SwitchboardMockDb", "name": "Mock DB"}}
        if node["type"] == "n8n-nodes-base.scheduleTrigger":
            node["type"] = "n8n-nodes-base.manualTrigger"
            node["typeVersion"] = 1
            node["parameters"] = {}
        if source.name == "wf_ingest_email.json" and node["name"] == "Gmail-shaped fixture":
            items = json.dumps([message.item() for message in messages])
            script = node["parameters"]["jsCode"]
            if replace_fixture:
                node["parameters"]["jsCode"] = f"return {items};"
            elif messages:
                if not script.endswith("];"):
                    raise StackError("Unexpected ingest fixture shape")
                node["parameters"]["jsCode"] = script[:-2] + "," + items[1:] + ";"
    return json.dumps(workflow)


@dataclass
class LiveStack:
    """A running mock stack. Use the methods to drive n8n and the reviewer fixture."""

    config: Settings
    credentials: StackCredentials = field(repr=False)
    core: str
    checkpoint_url: str
    history: Path
    slack: SlackFixture
    volume: str
    imports: Path
    _n8n_env: dict[str, str] = field(repr=False)

    def _rewrite(self, source: str) -> str:
        source = source.replace("http://host.docker.internal:8000", _in_container(self.core))
        return source.replace(
            "http://host.docker.internal:8001/checkpoint", _in_container(self.checkpoint_url)
        )

    def _write_workflow(self, target: Path, source: Path, messages: Sequence[EmailMessage]) -> None:
        live = target.parent.name == "live"
        target.write_text(
            self._rewrite(_workflow_copy(source, messages, replace_fixture=live)),
            encoding="utf-8",
        )

    def n8n(self, *command: str, expect_failure: bool = False, name: str | None = None) -> None:
        """Run one n8n CLI command in a throwaway container on the scratch volume.

        Raises StackError when the exit status is not the expected one. The message
        carries only status codes, node names and known hints: the CLI can dump
        complete node data, including ephemeral grants.
        """
        args = ["run", "--rm", "--pull", "never"]
        args += ["-v", f"{self.volume}:/home/node/.n8n", "-v", f"{self.imports}:/fixture:ro"]
        # Docker reads the generated values from the child environment rather than argv.
        for key in self._n8n_env:
            args.extend(("-e", key))
        if name:
            args.extend(("--name", name))
        try:
            result = subprocess.run(
                [*DOCKER, *args, IMAGE, *command],
                env={**os.environ, **self._n8n_env},
                text=True,
                capture_output=True,
                timeout=N8N_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            # The exception carries the captured output, which can hold grants. Drop it.
            raise StackError(
                f"n8n {' '.join(command)} timed out after {N8N_TIMEOUT_SECONDS} seconds"
            ) from None
        if (result.returncode != 0) == expect_failure:
            return
        output = result.stdout + result.stderr
        codes = sorted(set(re.findall(r'"httpCode":\s*"([1-5][0-9]{2})"', output)))
        nodes = sorted(set(re.findall(r'"node":\s*\{\s*"name":\s*"([A-Za-z .-]{1,80})"', output)))
        errors = sorted(set(re.findall(r"\b([A-Za-z]{2,40}(?:Error|Exception))\b", output)))
        workflow_nodes = {
            node["name"]
            for path in (self.imports / "workflows").glob("*.json")
            for node in json.loads(path.read_text(encoding="utf-8"))["nodes"]
        }
        hints = [hint for hint in N8N_FAILURE_HINTS if hint.casefold() in output.casefold()]
        references = sorted(node for node in workflow_nodes if node in output)
        raise StackError(
            f"n8n {' '.join(command)} exited {result.returncode}; "
            f"HTTP statuses: {codes}, nodes: {nodes}, errors: {errors}, "
            f"hints: {hints}, references: {references}"
        )

    def publish(self, *workflow_ids: str) -> None:
        for workflow_id in workflow_ids:
            self.n8n("publish:workflow", f"--id={workflow_id}")

    def send_email(self, sender: str, text: str, *, subject: str | None = None) -> str:
        """Send one new inbound email through wf_ingest_email. Returns its external ID.

        The harness's own import copy of the workflow is replaced with a copy whose
        manual fixture holds only this message. The exported workflow is untouched.
        """
        external_id = "switchboard-live-" + uuid4().hex
        message = EmailMessage(
            external_id,
            sender,
            text,
            date=datetime.now(UTC).isoformat(timespec="seconds"),
            subject=subject,
        )
        live = self.imports / "live"
        live.mkdir(exist_ok=True)
        self._write_workflow(
            live / "wf_ingest_email.json", ROOT / "workflows" / "wf_ingest_email.json", [message]
        )
        self.n8n("import:workflow", "--input=/fixture/live/wf_ingest_email.json")
        self.n8n("execute", "--id=SwitchboardEmail1")
        return external_id

    def interaction(
        self, review_id: object, decision: str = "approve", reviewer: str = REVIEWER
    ) -> None:
        """Submit a signed Slack button click for a review, as the given fixture reviewer."""
        payload = {
            "type": "block_actions",
            "api_app_id": self.config.slack_app_id,
            "team": {"id": WORKSPACE},
            "user": {"id": reviewer},
            "container": {"type": "message", "channel_id": CHANNEL, "message_ts": MESSAGE_TS},
            "actions": [
                {"type": "button", "action_id": f"switchboard_{decision}", "value": str(review_id)}
            ],
        }
        body = urlencode({"payload": json.dumps(payload)}).encode()
        stamp = str(int(time()))
        if not self.config.slack_signing_secret:
            raise StackError("Missing fixture Slack signing secret")
        signature = digest(
            self.config.slack_signing_secret.encode(),
            b"v0:" + stamp.encode() + b":" + body,
            "sha256",
        )
        result = httpx.post(
            self.core + "/approvals/slack",
            content=body,
            headers={
                "X-Slack-Request-Timestamp": stamp,
                "X-Slack-Signature": "v0=" + signature.hex(),
            },
        )
        if result.status_code != 200:
            raise StackError(f"Signed review interaction returned HTTP {result.status_code}")

    def checkpoints(self) -> list[dict[str, Any]]:
        lines = self.history.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines]


def _credentials_file(config: Settings, credentials: StackCredentials) -> str:
    headers = {
        "Worker": credentials.execution["worker"],
        "Reconciler": credentials.execution["reconciler"],
        "Approval": credentials.execution["approval_poster"],
        "Slack": credentials.slack_bot,
        "Checkpoint": credentials.schedule,
        "Ingress": credentials.ingress,
        "Understand": credentials.understand,
        "Retrieve": credentials.retrieve,
        "Draft": credentials.draft,
        "Decide": credentials.decide,
        "Lookup": credentials.lookup,
        "Refund": credentials.refund,
    }
    return json.dumps(
        [
            {
                "id": "SwitchboardMockDb",
                "name": "Mock DB",
                "type": "postgres",
                "data": {
                    "host": "host.docker.internal",
                    "port": config.db_port,
                    "database": config.db_name,
                    "user": "switchboard_n8n_mock",
                    "password": os.environ["SWITCHBOARD_N8N_MOCK_PASSWORD"],
                    "ssl": "disable",
                },
            },
            *[
                {
                    "id": f"Switchboard{role}Auth",
                    "name": f"Switchboard {role}",
                    "type": "httpHeaderAuth",
                    "data": {"name": "Authorization", "value": "Bearer " + secret},
                }
                for role, secret in headers.items()
            ],
        ]
    )


@contextmanager
def live_stack(
    config: Settings,
    credentials: StackCredentials,
    app: FastAPI,
    slack: SlackFixture,
    workdir: Path,
    *,
    extra_messages: Sequence[EmailMessage] = (),
    volume: str | None = None,
) -> Iterator[LiveStack]:
    """Serve ``app``, start the checkpoint process and import the workflows into scratch n8n.

    Nothing is published or executed here. On exit the core thread, the checkpoint
    process and the n8n volume are removed.
    """
    if not config.audit_token:
        raise StackError("Stack settings need an audit token")
    imports = workdir / "n8n-import"
    workflows = imports / "workflows"
    workflows.mkdir(parents=True)
    history = workdir / "audit.jsonl"
    with (
        serve(app) as core,
        checkpoint_process(
            history, core, config.audit_token, credentials.schedule
        ) as checkpoint_url,
    ):
        # A caller that must record the name before the volume exists passes it in.
        volume = volume or "switchboard-n8n-test-" + uuid4().hex
        stack = LiveStack(
            config=config,
            credentials=credentials,
            core=core,
            checkpoint_url=checkpoint_url,
            history=history,
            slack=slack,
            volume=volume,
            imports=imports,
            _n8n_env={
                "N8N_ENCRYPTION_KEY": token_hex(32),
                "N8N_BLOCK_ENV_ACCESS_IN_NODE": "true",
                "NODE_FUNCTION_ALLOW_BUILTIN": "crypto",
                "N8N_DIAGNOSTICS_ENABLED": "false",
                "N8N_VERSION_NOTIFICATIONS_ENABLED": "false",
                "N8N_USE_WORKFLOW_PUBLICATION_SERVICE": "false",
                "EXECUTIONS_DATA_SAVE_ON_ERROR": "none",
                "EXECUTIONS_DATA_SAVE_ON_SUCCESS": "none",
            },
        )
        for source in (ROOT / "workflows").glob("*.json"):
            stack._write_workflow(workflows / source.name, source, extra_messages)
        (imports / "credentials.json").write_text(
            _credentials_file(config, credentials), encoding="utf-8"
        )
        docker("volume", "create", volume)
        try:
            try:
                stack.n8n("import:credentials", "--input=/fixture/credentials.json")
            finally:
                # n8n now holds an encrypted copy. Do not keep the plain file around.
                (imports / "credentials.json").unlink()
            stack.n8n("import:workflow", "--separate", "--input=/fixture/workflows")
            yield stack
        finally:
            docker("volume", "rm", volume)


@contextmanager
def launch(
    base: Settings, workdir: Path, *, ingress_ttl_minutes: int, volume: str | None = None
) -> Iterator[LiveStack]:
    """Bring up the whole demo stack with every sub-workflow published."""
    if not base.offline_gateway_url:
        raise StackError("The live stack needs the offline AI gateway")
    config, credentials = stack_settings(base)
    prepare_tenant(config, credentials, ingress_ttl_minutes=ingress_ttl_minutes)
    slack = SlackFixture(credentials.slack_bot)
    app = create_app(config)
    slack.install(app)
    with live_stack(config, credentials, app, slack, workdir, volume=volume) as stack:
        stack.publish(*SUB_WORKFLOWS)
        yield stack
