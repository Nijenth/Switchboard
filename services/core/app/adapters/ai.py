"""One offline gateway request and its normalized accounting metadata."""

import json
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from http.client import HTTPException
from pathlib import Path
from typing import Any, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

import yaml

Provider = Literal["openai", "anthropic"]
Model = Literal["openai", "anthropic", "draft"]
OFFLINE_CONFIG = Path(__file__).resolve().parents[4] / "config/litellm/offline.yaml"


@dataclass(frozen=True)
class ModelLimits:
    max_input_tokens: int
    max_tokens: int
    input_price: Decimal
    output_price: Decimal

    def reservation(self) -> Decimal:
        return (
            self.max_input_tokens * self.input_price + self.max_tokens * self.output_price
        ).quantize(Decimal("0.000001"), rounding=ROUND_CEILING)


@dataclass(frozen=True)
class PreparedCall:
    alias: str
    body: bytes
    reservation: Decimal


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
        catalog = yaml.safe_load(OFFLINE_CONFIG.read_text(encoding="utf-8"))
        self.limits = {
            entry["model_name"]: ModelLimits(
                entry["model_info"]["max_input_tokens"],
                entry["model_info"]["max_output_tokens"],
                Decimal(str(entry["model_info"]["input_cost_per_token"])),
                Decimal(str(entry["model_info"]["output_cost_per_token"])),
            )
            for entry in catalog["model_list"]
        }

    def prepare(
        self,
        provider: Model,
        instructions: str,
        redacted: str,
        schema: dict[str, Any],
        *,
        knowledge: list[dict[str, str]] | None = None,
    ) -> PreparedCall | None:
        alias = f"switchboard-{provider}"
        limits = self.limits[alias]
        customer_json = json.dumps(redacted, ensure_ascii=True).replace("<", r"\u003c")
        messages = [
            {"role": "system", "content": instructions},
            {
                "role": "user",
                "content": f"<customer_message>\n{customer_json}\n</customer_message>",
            },
        ]
        if knowledge is not None:
            encoded = json.dumps(knowledge, ensure_ascii=True).replace("<", r"\u003c")
            messages.append({"role": "user", "content": f"<knowledge>\n{encoded}\n</knowledge>"})
        body = {
            "model": alias,
            "messages": messages,
            "max_tokens": limits.max_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "switchboard_result", "schema": schema, "strict": True},
            },
        }
        payload = json.dumps(body).encode()
        if len(payload) > limits.max_input_tokens:
            return None
        return PreparedCall(alias, payload, limits.reservation())

    def call(self, prepared: PreparedCall) -> Completion:
        alias = prepared.alias
        request = Request(
            self.url,
            prepared.body,
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
                cost if cost.is_finite() and 0 <= cost <= prepared.reservation else None,
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
