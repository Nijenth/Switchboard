"""One offline gateway request and its normalized accounting metadata."""

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from http.client import HTTPException
from typing import Any, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

Provider = Literal["openai", "anthropic"]
# The offline fixture charges a fixed amount per attempt, below this reservation.
RESERVATION = Decimal("0.01")


@dataclass(frozen=True)
class Completion:
    status: Literal["success", "timeout", "error"]
    model: str
    output: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost: Decimal | None = None
    retryable: bool = True


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class OfflineGateway:
    def __init__(self, base: str) -> None:
        url = urlsplit(base)
        if (
            url.scheme != "http"
            or url.hostname != "127.0.0.1"
            or url.netloc != f"127.0.0.1:{url.port}"
            or not url.port
            or url.path not in ("", "/")
            or url.query
            or url.fragment
        ):
            raise ValueError("Configure a loopback-only offline gateway URL")
        self.url = base.rstrip("/") + "/v1/chat/completions"
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def call(
        self, provider: Provider, instructions: str, redacted: str, schema: dict[str, Any]
    ) -> Completion:
        alias = f"switchboard-{provider}"
        customer_json = json.dumps(redacted, ensure_ascii=True).replace("<", r"\u003c")
        body = {
            "model": alias,
            "messages": [
                {"role": "system", "content": instructions},
                {
                    "role": "user",
                    "content": f"<customer_message>\n{customer_json}\n</customer_message>",
                },
            ],
            "max_tokens": 512,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "switchboard_result", "schema": schema, "strict": True},
            },
        }
        request = Request(
            self.url,
            json.dumps(body).encode(),
            {"Content-Type": "application/json"},
        )
        try:
            with self.opener.open(request, timeout=4) as response:
                result = json.loads(response.read(65_537))
                cost = Decimal(response.headers.get("x-litellm-response-cost", "NaN"))
            usage = result["usage"]
            output = result["choices"][0]["message"]["content"]
            input_tokens, output_tokens = usage["prompt_tokens"], usage["completion_tokens"]
            model = result["model"]
            if (
                not isinstance(model, str)
                or not model.isascii()
                or len(model) > 128
                or not isinstance(output, str)
                or type(input_tokens) is not int
                or type(output_tokens) is not int
                or not 0 <= input_tokens <= 2_147_483_647
                or not 0 <= output_tokens <= 2_147_483_647
            ):
                return Completion("error", alias)
            return Completion(
                "success",
                model,
                output,
                input_tokens,
                output_tokens,
                cost if cost.is_finite() and 0 <= cost <= RESERVATION else None,
            )
        except HTTPError as error:
            status: Literal["timeout", "error"] = "timeout" if error.code in (408, 504) else "error"
            return Completion(
                status, alias, retryable=error.code in (408, 429) or error.code >= 500
            )
        except TimeoutError:
            return Completion("timeout", alias)
        except (
            URLError,
            HTTPException,
            OSError,
            ValueError,
            TypeError,
            KeyError,
            IndexError,
            InvalidOperation,
        ):
            return Completion("error", alias)
