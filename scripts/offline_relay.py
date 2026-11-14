"""Expose only the isolated gateway's test endpoints to the host."""

import json
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from os import environ


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path != "/health/liveliness":
            self.send_error(404)
            return
        self.forward(None)

    def do_POST(self) -> None:
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        length = self.headers.get("Content-Length", "")
        if not length.isascii() or not length.isdecimal() or len(length) > 7:
            self.send_error(400)
            return
        if int(length) > 1_000_000:
            self.send_error(413)
            return
        body = self.rfile.read(int(length))
        try:
            request = json.loads(body)
            if (
                not isinstance(request, dict)
                or set(request) != {"model", "messages", "max_tokens", "response_format"}
                or request["model"]
                not in ("switchboard-openai", "switchboard-anthropic", "switchboard-draft")
            ):
                raise ValueError
        except (ValueError, RecursionError):
            self.send_error(400)
            return
        self.forward(body)

    def forward(self, body: bytes | None) -> None:
        conn = HTTPConnection("litellm", 4000, timeout=5)
        try:
            conn.request(
                self.command,
                self.path,
                body,
                {
                    "Content-Type": "application/json",
                    "Authorization": "Bearer " + environ["LITELLM_MASTER_KEY"],
                },
            )
            response = conn.getresponse()
            payload = response.read(65_537)
            if len(payload) > 65_536:
                self.send_error(502)
                return
            self.send_response(response.status)
            self.send_header("Content-Length", str(len(payload)))
            for name in ("Content-Type", "x-litellm-response-cost"):
                if value := response.getheader(name):
                    self.send_header(name, value)
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except OSError:
            self.send_error(503)
        finally:
            conn.close()

    def log_message(self, format: str, *args: object) -> None:
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 4000), Handler).serve_forever()
