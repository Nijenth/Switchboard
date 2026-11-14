import re
import unicodedata

from jsonschema import Draft202012Validator
from referencing import Registry

from app.guardrails import PII_PATTERNS, detect_prompt_injection, redact_text

DRAFT_SCHEMA = {
    "type": "object",
    "required": ["body", "citations"],
    "additionalProperties": False,
    "properties": {
        "body": {"type": "string", "minLength": 1, "maxLength": 2000},
        "citations": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
    },
}
PROMISE = re.compile(
    r"\b(?:guarantee\w*|promise\w*|definitely|no matter what)\b|"
    r"\b(?:we|i)\s+(?:will|shall)\s+(?:refund|replace|credit|deliver|ship)\b",
    re.IGNORECASE,
)
HOSTILE = re.compile(r"\b(?:idiot|stupid|shut up|your fault|moron)\b", re.IGNORECASE)
SENSITIVE = re.compile(r"\bNW-\d+\b|\[(?:EMAIL|PHONE|CARD|SSN|ADDRESS|SENDER)\]", re.IGNORECASE)
ORDER_REFERENCE = re.compile(r"NW-\d+", re.IGNORECASE)
TOOL_TOKEN = re.compile(r"(?<![\w@-])[A-Z0-9]{6,32}(?![\w@-])")
PLACEHOLDER = re.compile(r"\{([a-z][a-z0-9_]*)\}")
TOOL_PREFIX = "tool:"


def render_evidence(
    template: str,
    values: dict[str, object],
    schemas: dict[str, object] | None = None,
) -> str | None:
    """Render a pack evidence sentence from a lookup's input and result (ADR 0020).

    Returns None when any referenced value is missing, so incomplete results are never quoted.
    Values that pass a strict field schema (enum, pattern, format, const) are quoted verbatim;
    all other text is passed through PII redaction.
    """
    parts: list[str] = []
    last = 0
    for match in PLACEHOLDER.finditer(template):
        parts.append(redact_text(template[last : match.start()]))
        last = match.end()
        name = match.group(1)
        item = values.get(name)
        if item is None or isinstance(item, dict | list | bool):
            return None
        field_schema = schemas.get(name) if isinstance(schemas, dict) else None
        if (
            isinstance(field_schema, dict)
            and any(key in field_schema for key in ("enum", "pattern", "format", "const"))
            and Draft202012Validator(field_schema, registry=Registry()).is_valid(item)
        ):
            parts.append(str(item))
        else:
            parts.append(redact_text(str(item)))
    parts.append(redact_text(template[last:]))
    rendered = "".join(parts)
    return None if not rendered.strip() else rendered


def citation(key: str) -> dict[str, str]:
    if key.startswith(TOOL_PREFIX):
        return {"type": "tool", "action_id": key.removeprefix(TOOL_PREFIX)}
    return {"type": "chunk", "id": key}


def assess(
    body: str, citations: list[str], sources: dict[str, str], recipient: str
) -> tuple[list[dict[str, str]], dict[str, bool]]:
    """Automatic handling requires an exact, safe extract from cited evidence."""
    source_ids = list(dict.fromkeys(citations))
    valid_ids = bool(source_ids) and all(key in sources for key in source_ids)
    verified = [citation(key) for key in source_ids] if valid_ids else []
    # Order references and tracking tokens are allowed only when a cited tool result contains them.
    cited_orders = {
        hit.group(0).upper()
        for key in source_ids
        if key.startswith(TOOL_PREFIX) and key in sources
        for hit in SENSITIVE.finditer(sources[key])
        if ORDER_REFERENCE.fullmatch(hit.group(0))
    }
    cited_tokens = {
        hit.group(0)
        for key in source_ids
        if key.startswith(TOOL_PREFIX) and key in sources
        for hit in TOOL_TOKEN.finditer(sources[key])
    }
    normal = unicodedata.normalize("NFKC", body)
    scrubbed_body = TOOL_TOKEN.sub(
        lambda match: "TOKEN" if match.group(0) in cited_tokens else match.group(0), body
    )
    scrubbed_normal = unicodedata.normalize("NFKC", scrubbed_body)
    content = " ".join(body.split())

    def exact_sentence(source: str) -> bool:
        sentences = re.split(r"(?<=[.!?])\s+|\n+", source.strip())
        return content in {" ".join(sentence.split()) for sentence in sentences}

    grounded = valid_ids and all(exact_sentence(sources[key]) for key in source_ids)
    pii_safe = (
        "\x00" not in body
        and scrubbed_body == redact_text(scrubbed_body, sender_address=recipient)
        # An order reference may appear only when it comes from cited, verified evidence.
        and all(hit.group(0).upper() in cited_orders for hit in SENSITIVE.finditer(normal))
        and not any(pattern.search(scrubbed_normal) for _, pattern in PII_PATTERNS)
    )
    tone_ok = bool(re.search(r"[a-zA-Z]", normal)) and not (
        HOSTILE.search(normal) or normal.isupper() or "!!!" in normal
    )
    policy_ok = grounded and not (PROMISE.search(normal) or detect_prompt_injection(normal))
    return verified, {
        "g6_passed": valid_ids,
        "g7_passed": bool(pii_safe and policy_ok and tone_ok),
        "pii_safe": pii_safe,
        "policy_ok": bool(policy_ok),
        "tone_ok": tone_ok,
    }
