import re
import unicodedata

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
PLACEHOLDER = re.compile(r"\{([a-z][a-z0-9_]*)\}")
TOOL_PREFIX = "tool:"


def render_evidence(template: str, values: dict[str, object]) -> str | None:
    """Render a pack evidence sentence from a lookup's input and result (ADR 0020).

    Returns None when any referenced value is missing, so incomplete results are never quoted.
    """
    missing = False

    def value(match: re.Match[str]) -> str:
        nonlocal missing
        item = values.get(match.group(1))
        if item is None or isinstance(item, dict | list):
            missing = True
            return ""
        return str(item)

    rendered = PLACEHOLDER.sub(value, template)
    return None if missing or not rendered.strip() else rendered


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
    # Order references are allowed only when a cited tool result (not knowledge) contains them.
    cited_orders = {
        hit.group(0).upper()
        for key in source_ids
        if key.startswith(TOOL_PREFIX) and key in sources
        for hit in SENSITIVE.finditer(sources[key])
        if ORDER_REFERENCE.fullmatch(hit.group(0))
    }
    normal = unicodedata.normalize("NFKC", body)
    content = " ".join(body.split())

    def exact_sentence(source: str) -> bool:
        sentences = re.split(r"(?<=[.!?])\s+|\n+", source.strip())
        return content in {" ".join(sentence.split()) for sentence in sentences}

    grounded = valid_ids and all(exact_sentence(sources[key]) for key in source_ids)
    pii_safe = (
        "\x00" not in body
        and body == redact_text(body, sender_address=recipient)
        # An order reference may appear only when it comes from cited, verified evidence.
        and all(hit.group(0).upper() in cited_orders for hit in SENSITIVE.finditer(normal))
        and not any(pattern.search(normal) for _, pattern in PII_PATTERNS)
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
