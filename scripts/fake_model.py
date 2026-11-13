"""Deterministic upstream for the isolated LiteLLM profile."""

import json
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from app.guardrails import redact_text

FIXTURES = Path("/fixtures")


def fixtures() -> dict[str, str]:
    cases = FIXTURES / "cases.jsonl"
    if not cases.exists():
        return {}
    rows = (json.loads(line) for line in cases.read_text(encoding="utf-8").splitlines())
    return {
        redact_text(row["message"] or "", sender_address=row.get("sender_address") or ""): row[
            "expected_intent"
        ]
        for row in rows
    }


def completion(body: dict[str, Any], anthropic: bool) -> tuple[int, dict[str, Any]]:
    serialized = json.dumps(body)
    # Extract the actual string from the protocol, retaining JSON escapes inside the delimiter.
    for message in body["messages"]:
        content = message["content"]
        blocks = [{"text": content}] if isinstance(content, str) else content
        for block in blocks:
            text = block.get("text", "")
            if text.startswith("<customer_message>\n") and text.endswith("\n</customer_message>"):
                customer = json.loads(text.split("\n", 2)[1])
                break
        else:
            continue
        break
    else:
        return 400, {"error": {"message": "Fixture input has no customer delimiter"}}

    if "[fixture:timeout]" in customer and not anthropic:
        time.sleep(5)
    if ("[fixture:error]" in customer and not anthropic) or (
        "[fixture:anthropic-error]" in customer and anthropic
    ):
        return 503, {
            "error": {"message": "Fixture upstream unavailable", "type": "overloaded_error"}
        }
    if "[fixture:invalid-both]" in customer or ("[fixture:invalid]" in customer and not anthropic):
        output = "{invalid-json"
    elif "field_confidence" in serialized:
        order = re.search(r"\bNW-\d+\b", customer)
        output = json.dumps(
            {
                "data": {"order_number": order[0]} if order else {},
                "field_confidence": {"order_number": 0.99} if order else {},
            }
        )
    else:
        intent = fixtures().get(customer, "other")
        output = json.dumps({"intent_key": intent, "confidence": 0.99})

    if anthropic:
        return 200, {
            "id": "msg_fixture",
            "type": "message",
            "role": "assistant",
            "model": body["model"],
            "content": [{"type": "text", "text": output}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 100, "output_tokens": 20},
        }
    return 200, {
        "id": "chatcmpl-fixture",
        "object": "chat.completion",
        "created": 0,
        "model": body["model"],
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": output},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
    }


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        if self.path not in {"/v1/chat/completions", "/v1/messages"}:
            self.send_error(404)
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            status, response = completion(body, self.path == "/v1/messages")
        except (ValueError, TypeError, KeyError):
            self.send_error(400)
            return
        data = json.dumps(response).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, format: str, *args: object) -> None:
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
