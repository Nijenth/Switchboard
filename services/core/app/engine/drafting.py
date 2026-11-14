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


def assess(
    body: str, citations: list[str], sources: dict[str, str], recipient: str
) -> tuple[list[dict[str, str]], dict[str, bool]]:
    """Automatic handling requires an exact, safe extract from cited evidence."""
    source_ids = list(dict.fromkeys(citations))
    valid_ids = bool(source_ids) and all(key in sources for key in source_ids)
    verified = [{"type": "chunk", "id": key} for key in source_ids] if valid_ids else []
    normal = unicodedata.normalize("NFKC", body)
    content = " ".join(body.split())

    def exact_sentence(source: str) -> bool:
        sentences = re.split(r"(?<=[.!?])\s+|\n+", source.strip())
        return content in {" ".join(sentence.split()) for sentence in sentences}

    grounded = valid_ids and all(exact_sentence(sources[key]) for key in source_ids)
    pii_safe = (
        "\x00" not in body
        and body == redact_text(body, sender_address=recipient)
        and not SENSITIVE.search(normal)
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
