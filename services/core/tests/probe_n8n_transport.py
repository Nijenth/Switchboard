"""Run manually from services/core: python tests/probe_n8n_transport.py > probe.jsonl."""

import argparse
import json
import socket
import subprocess
import sys
from collections import Counter
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from threading import Thread
from time import monotonic, sleep, time_ns
from unittest.mock import patch
from urllib.request import urlopen
from uuid import uuid4

import uvicorn
from uvicorn.protocols.http.h11_impl import H11Protocol

SCRIPT = Path(__file__).resolve()
SERVER_IMAGE = "ghcr.io/berriai/litellm:v1.108.5"
NETWORK_PORT = 8765

VARIANTS = {
    "harness-one-shot": ("harness", False),
    "plain-one-shot": ("plain", False),
    "harness-long-lived": ("harness", True),
    "plain-long-lived": ("plain", True),
    "network-one-shot": ("network", False),
    "network-long-lived": ("network", True),
}


def event(kind, **fields):
    print(json.dumps({"event": kind, "time_ns": time_ns(), **fields}), flush=True)


def accepted(connection, peer):
    event("accept", peer=peer, port=connection.getsockname()[1])


@contextmanager
def instrument() -> Iterator[None]:
    """Observe the actual accept completion and the first bytes delivered to Uvicorn."""
    accept = socket.socket.accept

    def socket_accept(sock):
        connection, peer = accept(sock)
        accepted(connection, peer)
        return connection, peer

    def first_bytes(protocol, data):
        if not getattr(protocol, "_probe_received", False):
            protocol._probe_received = True
            request_line = data.split(b"\r\n", 1)[0].split(b" ")
            request_path = (
                request_line[1].decode("ascii", "replace") if len(request_line) >= 2 else None
            )
            event("first_bytes", peer=protocol.client, port=protocol.server[1], path=request_path)
        return data_received(protocol, data)

    data_received = H11Protocol.data_received
    with ExitStack() as stack:
        stack.enter_context(patch.object(socket.socket, "accept", socket_accept))
        stack.enter_context(patch.object(H11Protocol, "data_received", first_bytes))
        if sys.platform == "win32":
            from asyncio.windows_events import IocpProactor

            proactor_accept = IocpProactor.accept

            def accept_ex(proactor, listener):
                future = proactor_accept(proactor, listener)

                def completed(done):
                    if not done.cancelled() and done.exception() is None:
                        accepted(*done.result())

                future.add_done_callback(completed)
                return future

            stack.enter_context(patch.object(IocpProactor, "accept", accept_ex))
        yield


async def app(scope, receive, send):
    if scope["type"] != "http":
        return
    event("asgi", path=scope["path"], peer=scope["client"], port=scope["server"][1])
    await send(
        {"type": "http.response.start", "status": 200, "headers": [(b"content-length", b"2")]}
    )
    await send({"type": "http.response.body", "body": b"ok"})


@contextmanager
def plain_server(port=0):
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            loop="asyncio",
            http="h11",
            log_level="warning",
            access_log=False,
        )
    )
    thread = Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = monotonic() + 10
        while not server.started:
            if not thread.is_alive() or monotonic() >= deadline:
                raise RuntimeError("Plain Uvicorn did not start")
            sleep(0.02)
        yield f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def run(*, samples, batches, concurrency, reuse, variants):
    from harness.live_stack import DOCKER, IMAGE, serve

    def docker(*args, **kwargs):
        return subprocess.run(
            [*DOCKER, *args], check=True, text=True, capture_output=True, timeout=90, **kwargs
        ).stdout

    @contextmanager
    def container(name, *options):
        docker("run", "--detach", "--pull", "never", "--name", name, *options)
        try:
            yield name
        finally:
            docker("rm", "--force", name)

    client = SCRIPT.with_suffix(".js").read_text(encoding="utf-8")
    suffix = uuid4().hex[:12]
    for variant in variants:
        binding, persistent = VARIANTS[variant]
        with ExitStack() as stack:
            network = ()
            if binding == "network":
                # A shared network namespace lets the control stay loopback-only.
                # No host port is published and host.docker.internal is bypassed.
                name = stack.enter_context(
                    container(
                        f"switchboard-probe-{suffix}",
                        "--network",
                        "none",
                        "--mount",
                        f"type=bind,src={SCRIPT},dst=/probe.py,readonly",
                        "--entrypoint",
                        "python",
                        SERVER_IMAGE,
                        "-u",
                        "/probe.py",
                        "--network-server",
                    )
                )
                network = ("--network", f"container:{name}")
                deadline = monotonic() + 10
                while '"event": "ready"' not in docker("logs", name):
                    if monotonic() >= deadline:
                        raise RuntimeError("Network control did not start")
                    sleep(0.1)
                base = f"http://127.0.0.1:{NETWORK_PORT}"
            else:
                server = serve(app) if binding == "harness" else plain_server()
                host = stack.enter_context(server)
                with urlopen(f"{host}/ready", timeout=5) as response:
                    assert response.status == 200
                base = host.replace("127.0.0.1", "host.docker.internal")

            if persistent:
                client_name = stack.enter_context(
                    container(
                        f"switchboard-probe-client-{suffix}",
                        *network,
                        "--entrypoint",
                        "node",
                        IMAGE,
                        "-e",
                        "setInterval(() => {}, 10000)",
                    )
                )
                command = ("exec", "-i", client_name, "node")
            else:
                command = (
                    "run",
                    "--rm",
                    "--pull",
                    "never",
                    "-i",
                    *network,
                    "--entrypoint",
                    "node",
                    IMAGE,
                )

            event("variant", variant=variant, base=base)
            for index in range(samples):
                output = docker(
                    *command,
                    "-",
                    base,
                    f"{variant}-{index + 1}",
                    str(batches),
                    str(concurrency),
                    reuse,
                    input=client,
                )
                records = [json.loads(line) for line in output.splitlines()]
                assert len(records) == ((batches + 1) // 2) * 2 + (batches // 2) * concurrency
                for record in records:
                    print(json.dumps(record), flush=True)
                counts = Counter(
                    record.get("error", str(record.get("status"))) for record in records
                )
                event(
                    "sample",
                    variant=variant,
                    sample=index + 1,
                    counts=counts,
                    max_ms=max(record["done_ms"] for record in records),
                )
            if binding == "network":
                print(docker("logs", name), end="", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=12)
    parser.add_argument("--batches", type=int, default=12)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--reuse", action="store_true", help="enable HTTP keep-alive")
    parser.add_argument("--variant", choices=VARIANTS, action="append")
    args = parser.parse_args()
    if min(args.samples, args.batches, args.concurrency) < 1:
        parser.error("samples, batches and concurrency must be positive")
    with instrument():
        run(
            samples=args.samples,
            batches=args.batches,
            concurrency=args.concurrency,
            reuse="reuse" if args.reuse else "fresh",
            variants=args.variant or list(VARIANTS),
        )


if __name__ == "__main__":
    if sys.argv[1:] == ["--network-server"]:
        with instrument(), plain_server(NETWORK_PORT):
            event("ready")
            while True:
                sleep(1)
    else:
        # Running a script in tests/ does not include services/core on sys.path.
        sys.path.insert(0, str(SCRIPT.parents[1]))
        main()
