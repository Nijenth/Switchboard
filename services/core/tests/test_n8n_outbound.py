import json
import os
import re
import socket
import subprocess
import sys
from asyncio import to_thread
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from hashlib import sha256
from hmac import digest
from pathlib import Path
from secrets import token_hex
from threading import Thread
from time import monotonic, sleep, time
from urllib.parse import urlencode
from uuid import uuid4

import httpx
import psycopg
import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.responses import Response
from test_postgres import CASE_ID, NORTHWIND, set_northwind, verify_cluster

from app.adapters.db import Settings, create_pool
from app.main import create_app

ROOT = Path(__file__).resolve().parents[3]
IMAGE = "docker.n8n.io/n8nio/n8n:2.46.7"
WORKSPACE, REVIEWER, CHANNEL, MESSAGE_TS = "TWORKFLOW", "UWORKFLOW", "CWORKFLOW", "11.123456"
ADMIN = "20000000-0000-0000-0000-000000000004"


@contextmanager
def serve(app: FastAPI) -> Iterator[str]:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False))
        thread = Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        try:
            until = monotonic() + 10
            while not server.started and monotonic() < until:
                sleep(0.02)
            assert server.started
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
    child = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts" / "serve_audit_checkpoint.py")],
        env=env,
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
                    pytest.fail("checkpoint fixture exited before listening")
                sleep(0.05)
        else:
            pytest.fail("checkpoint fixture did not start")
        yield f"http://127.0.0.1:{port}/checkpoint"
    finally:
        child.terminate()
        child.wait(timeout=10)


def test_n8n_mock_outbound_loop(tmp_path: Path) -> None:
    if os.environ.get("SWITCHBOARD_TEST_N8N") != "1":
        pytest.skip("run scripts/test_core.ps1 -WithN8n")
    base = Settings.from_environment()
    tokens = {role: token_hex(32) for role in ("worker", "reconciler", "approval_poster")}
    ingress, understand, retrieve, draft, decide, lookup, refund = (token_hex(32) for _ in range(7))

    def scope(token: str) -> str:
        return json.dumps([{"tenant_id": str(NORTHWIND), "token": token}])

    config = replace(
        base,
        execution_credentials=json.dumps(
            [
                {"tenant_id": str(NORTHWIND), "role": role, "principal": role, "token": token}
                for role, token in tokens.items()
            ]
        ),
        audit_token=token_hex(32),
        understand_credentials=scope(understand),
        retrieve_credentials=scope(retrieve),
        draft_credentials=scope(draft),
        decide_credentials=scope(decide),
        lookup_credentials=scope(lookup),
        refund_credentials=scope(refund),
    )
    request_id, action_id, policy_id, draft_id = (uuid4() for _ in range(4))
    payment_review = uuid4()
    payment_case = uuid4()
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
        loader.execute(
            "UPDATE public.staff_users SET slack_user_id=%s WHERE tenant_id=%s AND id=%s",
            (REVIEWER, NORTHWIND, ADMIN),
        )
        loader.execute(
            """UPDATE public.queues SET notify_channel=%s
               WHERE tenant_id=%s AND key IN ('accounts_payable','customer_support')""",
            (CHANNEL, NORTHWIND),
        )
        loader.execute(
            """INSERT INTO public.ingress_credentials (token_digest,tenant_id,endpoint_id,expires_at)
               SELECT %s,tenant_id,id,now()+interval '30 minutes' FROM public.channel_endpoints
               WHERE tenant_id=%s AND address='support@northwind.example'""",
            (sha256(ingress.encode()).digest(), NORTHWIND),
        )
    with create_pool(config) as pool, pool.connection() as conn, conn.transaction():
        set_northwind(conn)
        conn.execute(
            """UPDATE public.cases SET queue_id=(SELECT id FROM public.queues WHERE tenant_id=%s
                 AND key='accounts_payable') WHERE tenant_id=%s AND id=%s""",
            (NORTHWIND, NORTHWIND, CASE_ID),
        )
        conn.execute(
            """INSERT INTO public.policy_decisions (id,tenant_id,case_id,pack_id,trace_id,decision,
                 rule_id,rule_source,reason,policy_version,required_role)
               SELECT %s,tenant_id,id,pack_id,%s,'needs_approval','fixture','pack','Review','1','finance'
                 FROM public.cases WHERE tenant_id=%s AND id=%s""",
            (policy_id, uuid4(), NORTHWIND, CASE_ID),
        )
        conn.execute(
            """INSERT INTO public.drafts (id,tenant_id,case_id,policy_decision_id,channel,recipient,body)
               VALUES (%s,%s,%s,%s,'email','fixture@example.test','Synthetic reply')""",
            (draft_id, NORTHWIND, CASE_ID, policy_id),
        )
        conn.execute(
            """INSERT INTO public.actions (id,tenant_id,case_id,policy_decision_id,trace_id,idempotency_key,
                 tool_key,risk_level,status,input)
               VALUES (%s,%s,%s,%s,%s,%s,'send_reply','external_comm','pending_approval',
                 jsonb_build_object('draft_id',%s::uuid))""",
            (action_id, NORTHWIND, CASE_ID, policy_id, uuid4(), str(action_id), draft_id),
        )
        for kind, subject_id, review_id in (
            ("draft", draft_id, request_id),
            ("action", action_id, uuid4()),
        ):
            conn.execute(
                """INSERT INTO public.approval_requests (id,tenant_id,case_id,subject_type,subject_id,reason)
                   VALUES (%s,%s,%s,%s,%s,'Fixture review')""",
                (review_id, NORTHWIND, CASE_ID, kind, subject_id),
            )
            if kind == "action":
                action_review_id = review_id
        conn.execute(
            """INSERT INTO public.cases (id,tenant_id,tenant_pack_id,pack_id,channel,queue_id,endpoint_id)
               SELECT %s,e.tenant_id,e.tenant_pack_id,tp.pack_id,e.channel,q.id,e.id
               FROM public.channel_endpoints e JOIN public.tenant_packs tp
                 ON (tp.tenant_id,tp.id)=(e.tenant_id,e.tenant_pack_id)
               JOIN public.queues q ON q.tenant_id=e.tenant_id AND q.key='accounts_payable'
               WHERE e.tenant_id=%s AND e.address='invoices@northwind.example'""",
            (payment_case, NORTHWIND),
        )
        conn.execute(
            """INSERT INTO public.actions
               (id,tenant_id,case_id,trace_id,idempotency_key,tool_key,risk_level,status,amount,currency)
               SELECT %s,tenant_id,id,%s,%s,'invoice_schedule_payment','money','pending_approval',5,'USD'
               FROM public.cases WHERE tenant_id=%s AND id=%s""",
            (payment_review, uuid4(), str(payment_review), NORTHWIND, payment_case),
        )
        conn.execute(
            """INSERT INTO public.approval_requests (id,tenant_id,case_id,subject_type,subject_id,reason)
               VALUES (%s,%s,%s,'action',%s,'Fixture payment review')""",
            (payment_review, NORTHWIND, payment_case, payment_review),
        )

    app = create_app(config)
    posted: set[str] = set()
    slack_bot_token = token_hex(32)
    completion_lost = False
    kill_worker: Callable[[], None] | None = None
    lost_action: object = action_id

    @app.middleware("http")
    async def lose_first_completion(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        nonlocal completion_lost
        if request.url.path == f"/actions/{lost_action}/complete" and not completion_lost:
            completion_lost = True
            if kill_worker is not None:
                await to_thread(kill_worker)
            return JSONResponse({"detail": "Fixture connection lost"}, status_code=503)
        return await call_next(request)

    @app.post("/fixture/slack/chat.postMessage")
    async def fake_slack(request: Request) -> dict[str, object]:
        assert request.headers.get("Authorization") == "Bearer " + slack_bot_token
        payload = await request.json()
        assert payload["channel"] == CHANNEL
        button = payload["blocks"][-1]["elements"][0]
        assert button["action_id"] == "switchboard_approve"
        posted.add(button["value"])
        return {"ok": True, "channel": CHANNEL, "ts": MESSAGE_TS}

    imports = tmp_path / "n8n-import"
    workflows = imports / "workflows"
    workflows.mkdir(parents=True)
    for source in (ROOT / "workflows").glob("*.json"):
        workflow = json.loads(source.read_text(encoding="utf-8"))
        for node in workflow["nodes"]:
            if node["type"] == "n8n-nodes-base.postgres":
                node["credentials"] = {"postgres": {"id": "SwitchboardMockDb", "name": "Mock DB"}}
            if node["type"] == "n8n-nodes-base.scheduleTrigger":
                node["type"] = "n8n-nodes-base.manualTrigger"
                node["typeVersion"] = 1
                node["parameters"] = {}
        (workflows / source.name).write_text(json.dumps(workflow), encoding="utf-8")

    def docker(*args: str) -> None:
        result = subprocess.run(
            ["docker", "--context", "desktop-linux", *args],
            text=True,
            capture_output=True,
            timeout=80,
            check=False,
        )
        assert result.returncode == 0, (result.stdout + result.stderr)[-4000:]

    def interaction(core: str, review_id: object, decision: str = "approve") -> None:
        payload = {
            "type": "block_actions",
            "api_app_id": config.slack_app_id,
            "team": {"id": WORKSPACE},
            "user": {"id": REVIEWER},
            "container": {"type": "message", "channel_id": CHANNEL, "message_ts": MESSAGE_TS},
            "actions": [
                {"type": "button", "action_id": f"switchboard_{decision}", "value": str(review_id)}
            ],
        }
        body = urlencode({"payload": json.dumps(payload)}).encode()
        stamp = str(int(time()))
        assert config.slack_signing_secret
        signature = digest(
            config.slack_signing_secret.encode(), b"v0:" + stamp.encode() + b":" + body, "sha256"
        )
        result = httpx.post(
            core + "/approvals/slack",
            content=body,
            headers={
                "X-Slack-Request-Timestamp": stamp,
                "X-Slack-Signature": "v0=" + signature.hex(),
            },
        )
        assert result.status_code == 200, result.text

    assert config.audit_token
    history = tmp_path / "audit.jsonl"
    schedule_token = token_hex(32)
    with (
        serve(app) as core,
        checkpoint_process(history, core, config.audit_token, schedule_token) as checkpoint_url,
    ):
        core_url = core.replace("127.0.0.1", "host.docker.internal")
        for file in workflows.glob("*.json"):
            source = file.read_text(encoding="utf-8")
            source = source.replace("http://host.docker.internal:8000", core_url)
            source = source.replace(
                "http://host.docker.internal:8001/checkpoint",
                checkpoint_url.replace("127.0.0.1", "host.docker.internal"),
            )
            file.write_text(source, encoding="utf-8")
        credentials_file = imports / "credentials.json"
        credentials_file.write_text(
            json.dumps(
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
                        for role, secret in {
                            "Worker": tokens["worker"],
                            "Reconciler": tokens["reconciler"],
                            "Approval": tokens["approval_poster"],
                            "Slack": slack_bot_token,
                            "Checkpoint": schedule_token,
                            "Ingress": ingress,
                            "Understand": understand,
                            "Retrieve": retrieve,
                            "Draft": draft,
                            "Decide": decide,
                            "Lookup": lookup,
                            "Refund": refund,
                        }.items()
                    ],
                ]
            ),
            encoding="utf-8",
        )
        volume = "switchboard-n8n-test-" + uuid4().hex
        docker("volume", "create", volume)
        try:
            n8n_env = {
                "N8N_ENCRYPTION_KEY": token_hex(32),
                "N8N_BLOCK_ENV_ACCESS_IN_NODE": "true",
                "NODE_FUNCTION_ALLOW_BUILTIN": "crypto",
                "N8N_DIAGNOSTICS_ENABLED": "false",
                "N8N_VERSION_NOTIFICATIONS_ENABLED": "false",
                "N8N_USE_WORKFLOW_PUBLICATION_SERVICE": "false",
                "EXECUTIONS_DATA_SAVE_ON_ERROR": "none",
                "EXECUTIONS_DATA_SAVE_ON_SUCCESS": "none",
            }
            args = [
                "run",
                "--rm",
                "--pull",
                "never",
                "-v",
                f"{volume}:/home/node/.n8n",
                "-v",
                f"{imports}:/fixture:ro",
            ]
            # Docker reads the generated values from the child environment rather than argv.
            for key in n8n_env:
                args.extend(("-e", key))

            def n8n(*command: str, expect_failure: bool = False, name: str | None = None) -> None:
                run_args = [*args, "--name", name] if name else args
                result = subprocess.run(
                    ["docker", "--context", "desktop-linux", *run_args, IMAGE, *command],
                    env={**os.environ, **n8n_env},
                    text=True,
                    capture_output=True,
                    timeout=100,
                    check=False,
                )
                if (result.returncode != 0) != expect_failure or (
                    expect_failure and not completion_lost
                ):
                    # The CLI can dump complete node data, including ephemeral grants.
                    output = result.stdout + result.stderr
                    codes = sorted(set(re.findall(r'"httpCode":\s*"([1-5][0-9]{2})"', output)))
                    nodes = sorted(
                        set(re.findall(r'"node":\s*\{\s*"name":\s*"([A-Za-z .-]{1,80})"', output))
                    )
                    errors = sorted(
                        set(re.findall(r"\b([A-Za-z]{2,40}(?:Error|Exception))\b", output))
                    )
                    workflow_nodes = {
                        node["name"]
                        for path in workflows.glob("*.json")
                        for node in json.loads(path.read_text(encoding="utf-8"))["nodes"]
                    }
                    hints = [
                        hint
                        for hint in (
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
                        if hint.casefold() in output.casefold()
                    ]
                    references = sorted(name for name in workflow_nodes if name in output)
                    pytest.fail(
                        f"n8n {' '.join(command)} exited {result.returncode}; "
                        f"HTTP statuses: {codes}, nodes: {nodes}, errors: {errors}, "
                        f"hints: {hints}, references: {references}"
                    )

            n8n("import:credentials", "--input=/fixture/credentials.json")
            n8n("import:workflow", "--separate", "--input=/fixture/workflows")
            n8n("publish:workflow", "--id=SwitchboardError1")
            n8n("publish:workflow", "--id=SwitchboardSend1")
            n8n("publish:workflow", "--id=SwitchboardRefundIssue1")
            n8n("publish:workflow", "--id=SwitchboardConfig1")
            n8n("execute", "--id=SwitchboardSlack1")
            assert posted == {str(request_id), str(action_review_id), str(payment_review)}
            interaction(core, request_id)
            interaction(core, action_review_id)
            worker_name = "switchboard-crash-" + uuid4().hex
            kill_worker = lambda: docker("kill", worker_name)
            n8n("execute", "--id=SwitchboardDispatch1", expect_failure=True, name=worker_name)
            assert completion_lost
            with psycopg.connect(
                host=config.db_host,
                port=config.db_port,
                dbname=config.db_name,
                user="switchboard_owner",
                password=os.environ["SWITCHBOARD_DB_OWNER_PASSWORD"],
                connect_timeout=3,
            ) as owner:
                verify_cluster(owner, "switchboard_owner")
                owner.execute("ALTER TABLE public.actions DISABLE TRIGGER actions_enforce_rules")
                owner.execute(
                    "UPDATE public.actions SET lease_expires_at=clock_timestamp() WHERE tenant_id=%s AND id=%s",
                    (NORTHWIND, action_id),
                )
                owner.execute("ALTER TABLE public.actions ENABLE TRIGGER actions_enforce_rules")
            n8n("execute", "--id=SwitchboardReconcile1")
            n8n("execute", "--id=SwitchboardDispatch1")
            n8n("execute", "--id=SwitchboardAudit1")
            if config.offline_gateway_url:
                for workflow_id in (
                    "SwitchboardIdentity1",
                    "SwitchboardOrder1",
                    "SwitchboardRefundEligibility1",
                    "SwitchboardLookups1",
                    "SwitchboardCase1",
                ):
                    n8n("publish:workflow", f"--id={workflow_id}")
                n8n("execute", "--id=SwitchboardEmail1")
                posted.clear()
                n8n("execute", "--id=SwitchboardSlack1")
                with create_pool(config) as pool, pool.connection() as conn, conn.transaction():
                    set_northwind(conn)
                    refund_review = conn.execute(
                        """
                        SELECT r.id::text,a.amount,a.currency,r.required_role,a.id
                        FROM public.actions a JOIN public.approval_requests r
                          ON (r.tenant_id,r.case_id,r.subject_id)=(a.tenant_id,a.case_id,a.id)
                        WHERE a.tenant_id=%s AND a.tool_key='order_refund_issue'
                        """,
                        (NORTHWIND,),
                    ).fetchone()
                    assert refund_review is not None
                    assert refund_review[1:4] == (210, "EUR", "manager")
                assert refund_review[0] in posted
                for review_id in sorted(posted):
                    interaction(
                        core, review_id, "approve" if review_id == refund_review[0] else "reject"
                    )
                lost_action = refund_review[4]
                completion_lost = False
                worker_name = "switchboard-refund-crash-" + uuid4().hex
                n8n("execute", "--id=SwitchboardDispatch1", expect_failure=True, name=worker_name)
                assert completion_lost
                with psycopg.connect(
                    host=config.db_host,
                    port=config.db_port,
                    dbname=config.db_name,
                    user="switchboard_owner",
                    password=os.environ["SWITCHBOARD_DB_OWNER_PASSWORD"],
                    connect_timeout=3,
                ) as owner:
                    verify_cluster(owner, "switchboard_owner")
                    owner.execute(
                        "ALTER TABLE public.actions DISABLE TRIGGER actions_enforce_rules"
                    )
                    owner.execute(
                        "UPDATE public.actions SET lease_expires_at=clock_timestamp() WHERE tenant_id=%s AND id=%s",
                        (NORTHWIND, lost_action),
                    )
                    owner.execute("ALTER TABLE public.actions ENABLE TRIGGER actions_enforce_rules")
                n8n("execute", "--id=SwitchboardReconcile1")
                n8n("execute", "--id=SwitchboardDispatch1")
                n8n("execute", "--id=SwitchboardSla1")
                with create_pool(config) as pool, pool.connection() as conn, conn.transaction():
                    set_northwind(conn)
                    assert conn.execute(
                        """
                        SELECT a.status,d.status FROM public.messages m
                        JOIN public.drafts d ON (d.tenant_id,d.in_reply_to_message_id)=(m.tenant_id,m.id)
                            JOIN public.actions a ON a.tenant_id=d.tenant_id AND a.input->>'draft_id'=d.id::text
                        WHERE m.tenant_id=%s AND m.external_message_id='switchboard-synthetic-shipping-1'
                        """,
                        (NORTHWIND,),
                    ).fetchall() == [("proposed", "rejected")]
                    refund_result = conn.execute(
                        """
                        SELECT status,output->>'status',external_ref=output->>'refund_reference'
                        FROM public.actions WHERE tenant_id=%s AND tool_key='order_refund_issue'
                        """,
                        (NORTHWIND,),
                    ).fetchall()
                    assert refund_result == [("executed", "refunded", True)]
                    assert conn.execute(
                        """SELECT count(*) FROM public.audit_log
                           WHERE tenant_id=%s AND entity_id=%s AND event_type='action.reconciled'""",
                        (NORTHWIND, lost_action),
                    ).fetchone() == (1,)
                    # The order email ran its lookups and produced an automatic, tool-cited reply.
                    lookups = conn.execute(
                        """
                        SELECT a.id,a.tool_key,a.status,c.identity_verified FROM public.messages m
                        JOIN public.cases c ON (c.tenant_id,c.id)=(m.tenant_id,m.case_id)
                        JOIN public.actions a ON (a.tenant_id,a.message_id)=(m.tenant_id,m.id)
                        WHERE m.tenant_id=%s AND m.external_message_id='switchboard-synthetic-order-1'
                          AND a.risk_level='read' ORDER BY a.executed_at,a.id
                        """,
                        (NORTHWIND,),
                    ).fetchall()
                    assert lookups[0][1] == "order_identity_check"
                    assert {
                        (tool_key, status, verified) for _, tool_key, status, verified in lookups
                    } == {
                        ("order_identity_check", "executed", True),
                        ("order_lookup", "executed", True),
                        ("order_refund_eligibility", "executed", True),
                    }
                    lookup_id = next(action for action, key, *_ in lookups if key == "order_lookup")
                    order_reply = conn.execute(
                        """
                        SELECT p.decision,p.rule_id,p.inputs->'checks',a.status,d.status,d.body,d.citations,
                               (SELECT count(*) FROM public.approval_requests r
                                WHERE (r.tenant_id,r.case_id)=(m.tenant_id,m.case_id))
                        FROM public.messages m
                        JOIN public.drafts d ON (d.tenant_id,d.in_reply_to_message_id)=(m.tenant_id,m.id)
                        JOIN public.policy_decisions p ON (p.tenant_id,p.id)=(d.tenant_id,d.policy_decision_id)
                        JOIN public.actions a ON (a.tenant_id,a.execution_draft_id)=(d.tenant_id,d.id)
                        WHERE m.tenant_id=%s AND m.external_message_id='switchboard-synthetic-order-1'
                        """,
                        (NORTHWIND,),
                    ).fetchone()
                    assert order_reply is not None
                    (
                        decision,
                        rule_id,
                        checks,
                        act_status,
                        draft_status,
                        body,
                        citations,
                        reviews,
                    ) = order_reply
                    assert (decision, rule_id, act_status, draft_status, reviews) == (
                        "auto",
                        "ORDER_STATUS",
                        "executed",
                        "sent",
                        0,
                    )
                    assert (checks["order_status"], checks["carrier"]) == ("shipped", "UPS")
                    assert body == (
                        "Your order NW-1001 is shipped with UPS, tracking number 1Z999AA10000000001."
                    )
                    assert citations == [{"type": "tool", "action_id": str(lookup_id)}]
            checkpoints = [
                json.loads(line) for line in history.read_text(encoding="utf-8").splitlines()
            ]
            assert len(checkpoints) == 2
            assert checkpoints[-1]["chain_seq"] > checkpoints[0]["chain_seq"]
        finally:
            docker("volume", "rm", volume)

    with create_pool(config) as pool, pool.connection() as conn, conn.transaction():
        set_northwind(conn)
        row = conn.execute(
            """SELECT a.status, d.status, a.external_ref FROM public.actions a
                JOIN public.drafts d ON (d.tenant_id,d.id)=(a.tenant_id,a.execution_draft_id)
                WHERE a.tenant_id=%s AND a.id=%s""",
            (NORTHWIND, action_id),
        ).fetchone()
        assert row is not None and row[:2] == ("executed", "sent")
        external_ref = row[2]
    with psycopg.connect(
        host=config.db_host,
        port=config.db_port,
        dbname=config.db_name,
        user="switchboard_n8n_mock",
        password=os.environ["SWITCHBOARD_N8N_MOCK_PASSWORD"],
        connect_timeout=3,
    ) as mock:
        verify_cluster(mock, "switchboard_n8n_mock")
        assert mock.execute(
            "SELECT external_ref::text FROM demo.outbound_receipts WHERE tenant_id=%s AND action_id=%s",
            (NORTHWIND, action_id),
        ).fetchall() == [(external_ref,)]
        if config.offline_gateway_url:
            assert mock.execute(
                """SELECT r.amount,r.currency,o.status
                   FROM demo.order_refunds r JOIN demo.shop_orders o USING (tenant_id,order_number)
                   WHERE r.tenant_id=%s AND r.order_number='NW-1003'""",
                (NORTHWIND,),
            ).fetchall() == [(210, "EUR", "refunded")]
