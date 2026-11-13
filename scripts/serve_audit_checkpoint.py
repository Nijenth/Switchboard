#!/usr/bin/env python3
import json
import os
from hmac import compare_digest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from checkpoint_audit import advance


def make_handler(history, audit_url, audit_token, schedule_token):
    class CheckpointHandler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_POST(self):
            if self.path != "/checkpoint":
                self.send_error(404)
                return
            authorization = self.headers.get("Authorization", "")
            if not authorization.isascii() or not compare_digest(
                authorization, "Bearer " + schedule_token
            ):
                self.send_error(403)
                return
            try:
                seq = advance(history, audit_url, audit_token)
            except (OSError, ValueError, KeyError, TypeError):
                self.send_error(503, "Checkpoint could not be advanced")
                return
            data = json.dumps({"chain_seq": seq}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return CheckpointHandler


if __name__ == "__main__":
    audit_token = os.environ["SWITCHBOARD_AUDIT_TOKEN"]
    schedule_token = os.environ["SWITCHBOARD_CHECKPOINT_SCHEDULE_TOKEN"]
    if (
        not schedule_token.isascii()
        or len(schedule_token) < 32
        or schedule_token == audit_token
    ):
        raise SystemExit("Configure a separate checkpoint schedule credential")
    handler = make_handler(
        Path(os.environ["SWITCHBOARD_CHECKPOINT_HISTORY"]),
        os.environ["SWITCHBOARD_AUDIT_URL"],
        audit_token,
        schedule_token,
    )
    address = os.environ.get("SWITCHBOARD_CHECKPOINT_BIND", "127.0.0.1")
    port = int(os.environ.get("SWITCHBOARD_CHECKPOINT_PORT", "8766"))
    with HTTPServer((address, port), handler) as server:
        server.serve_forever()
