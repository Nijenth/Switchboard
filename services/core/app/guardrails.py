"""Offline checks on untrusted customer text."""

import re
import unicodedata
from dataclasses import dataclass

PII_PATTERNS = (
    ("EMAIL", re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+", re.IGNORECASE)),
    ("SSN", re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")),
    ("CARD", re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?![ -]?\d)")),
    (
        "PHONE",
        re.compile(
            r"(?<![\w+])(?:\+\d{1,3}[ .-]?)?(?:\(\d{3}\)|\d{3})[ .-]\d{3}[ .-]\d{4}(?!\d)"
            r"|(?<![\w+])\+\d(?:[ .-]?\d){7,14}(?![ .-]?\d)"
            r"|(?<![\w+-])\d{10}(?![\w-])"
        ),
    ),
)

INJECTION_PATTERNS = (
    re.compile(
        r"\b(?:ignore|disregard|forget|override|bypass)\b.{0,70}"
        r"\b(?:previous|prior|above|all|system|developer|safety)\b.{0,35}"
        r"\b(?:instructions?|messages?|prompts?|rules?|polic(?:y|ies))\b"
    ),
    re.compile(
        r"\b(?:reveal|print|repeat|show|disclose|output)\b.{0,45}"
        r"\b(?:system|developer|hidden)\s+(?:prompt|instructions?)\b"
    ),
    re.compile(r"</?(?:system|developer|customer_message)\b|\[/?inst\]|<\|(?:im_start|system)\|>"),
)

LANGUAGE_WORDS = {
    "en": frozenset(
        ["hello", "please", "where", "my", "the", "is", "thank", "thanks", "with", "your"]
    ),
    "es": frozenset(
        ["hola", "gracias", "dónde", "donde", "está", "por", "favor", "mi", "pedido", "para"]
    ),
    "fr": frozenset(
        ["bonjour", "merci", "où", "est", "mon", "commande", "avec", "votre", "veuillez"]
    ),
    "de": frozenset(
        ["hallo", "danke", "wo", "ist", "mein", "meine", "bitte", "bestellung", "nicht"]
    ),
    "pt": frozenset(
        ["olá", "obrigado", "obrigada", "onde", "está", "meu", "minha", "encomenda", "vocês"]
    ),
}


@dataclass(frozen=True)
class GuardrailResult:
    body_redacted: str
    suspected_injection: bool
    language: str


def redact_text(text: str, *, sender_name: str = "", sender_address: str = "") -> str:
    text = unicodedata.normalize("NFKC", text)
    text = "".join(char for char in text if unicodedata.category(char) != "Cf")
    for label, pattern in PII_PATTERNS:
        text = pattern.sub(f"[{label}]", text)
    for value in (sender_address, sender_name):
        known_value = unicodedata.normalize("NFKC", value).strip()
        if len(known_value) >= 2:
            text = re.sub(
                rf"(?<!\w){re.escape(known_value)}(?!\w)", "[SENDER]", text, flags=re.IGNORECASE
            )
    return text


def detect_prompt_injection(text: str) -> bool:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    visible = "".join(char for char in normalized if unicodedata.category(char) != "Cf")
    compact = " ".join(visible.split())
    return any(pattern.search(compact) for pattern in INJECTION_PATTERNS)


def detect_language(redacted_text: str) -> str:
    words = set(re.findall(r"[^\W\d_]+", redacted_text.casefold()))
    scores = sorted(
        ((len(words & vocabulary), language) for language, vocabulary in LANGUAGE_WORDS.items()),
        reverse=True,
    )
    best, language = scores[0]
    return language if best >= 2 and best > scores[1][0] else "und"


def inspect_message(
    text: str, *, sender_name: str = "", sender_address: str = ""
) -> GuardrailResult:
    redacted = redact_text(text, sender_name=sender_name, sender_address=sender_address)
    return GuardrailResult(redacted, detect_prompt_injection(text), detect_language(redacted))
