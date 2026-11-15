"""Offline checks on untrusted customer text."""

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass

# Keep title case significant for names and city candidates, even when matching a cue
# case-insensitively. Horizontal whitespace keeps matches inside an address or name line.
NAME_WORD = r"[A-ZÀ-ÖØ-Þ][a-zà-öø-ÿ]+(?:[-'’][A-ZÀ-ÖØ-Þa-zà-öø-ÿ]+)*"
PERSON_NAME = rf"{NAME_WORD}(?:[ \t]+(?:(?:de|da|do|del|van|von)[ \t]+)?{NAME_WORD}){{0,3}}"
GREETING = r"hi|hello|dear|bonjour|salut|hola|hallo|guten tag|olá|ola"
FIRST_NAME_GREETING = re.compile(rf"\A(?i:{GREETING})[ \t]+{NAME_WORD},")
NAME_PATTERNS = (
    re.compile(
        rf"\b(?P<explicit>(?i:my name is|je m['’]appelle|me llamo|ich heiße|meu nome é|c/o))[ \t]+"
        rf"(?P<name>{PERSON_NAME})(?![\w@])"
    ),
    re.compile(
        rf"\b(?i:for)[ \t]+(?P<name>{NAME_WORD}(?:[ \t]+{NAME_WORD}){{1,3}})"
        r"(?=[ \t]*(?:[.,!?;]|$)|[ \t]+(?:to|at|on)\b)"
    ),
    re.compile(rf"(?m:^[ \t]*)(?i:{GREETING})[ \t]+(?P<name>{PERSON_NAME})(?=,)"),
    re.compile(
        r"^[ \t]*(?i:thanks|thank you|regards|best regards|merci|cordialement|"
        r"gracias|saludos|danke|viele grüße|mit freundlichen grüßen|obrigad[oa]|cumprimentos)"
        rf"(?:[ \t]*,[ \t]*|[ \t]*\r?\n[ \t]*)(?P<name>{PERSON_NAME})(?=[ \t]*[.!]?[ \t]*$)",
        re.MULTILINE,
    ),
)
NON_PERSON_TERMS = frozenset(
    [
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
        "january",
        "february",
        "march",
        "april",
        "may",
        "june",
        "july",
        "august",
        "september",
        "october",
        "november",
        "december",
        "paris",
        "madrid",
        "berlin",
        "lisboa",
        "london",
        "ups",
        "dhl",
        "fedex",
    ]
)
PRODUCT_TERMS = frozenset(
    ["apparel", "jacket", "cap", "coat", "shirt", "street", "road", "avenue", "lane"]
)

HOUSE_NUMBER = r"\d{1,6}[A-Za-z]?(?:[-/]\d{1,6}[A-Za-z]?)?"
STREET_WORD = r"[^\W\d_]+(?:[-'’][^\W\d_]+)*"
ROMANCE_STREET = r"(?i:rue|avenue|calle|avenida|rua|via)"
ROMANCE_STREET_NAME = rf"{ROMANCE_STREET}(?:[ \t]+(?:{NAME_WORD}|de|des|du|del|la|las|los|da|das|do|dos|dei|della)){{1,6}}"
GERMAN_STREET = (
    rf"(?:{NAME_WORD}[ \t]+){{0,3}}{STREET_WORD}(?i:straße|strasse|str\.|weg|platz|gasse)"
)
GERMAN_STREET = (
    rf"(?:{GERMAN_STREET}|(?:{NAME_WORD}[ \t]+){{1,3}}(?i:straße|strasse|str\.|weg|platz|gasse))"
)
POSTCODE = (
    r"(?i:[A-Z]{1,2}\d[A-Z\d]?[ \t]+\d[A-Z]{2}|[A-Z]\d[A-Z][ \t]+\d[A-Z]\d)"
    r"|\d{4}-\d{3}|\d{5}(?:-\d{4})?"
)

PII_PATTERNS = (
    ("EMAIL", re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+", re.IGNORECASE)),
    ("SSN", re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")),
    # An explicit international prefix distinguishes this from a grouped card number.
    (
        "PHONE",
        re.compile(
            r"(?<![\w+])\+\d{1,3}[ .-]?\(\d{2,4}\)(?:[ .-]?\d){5,10}(?![ .-]?\d)"
            r"|(?<![\w+])\+\d(?:[ .-]?\d){7,14}(?![ .-]?\d)"
        ),
    ),
    ("CARD", re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?![ -]?\d)")),
    (
        "ADDRESS",
        re.compile(
            r"(?<![\w-])\d{1,6}[ \t]+(?:[\w'-]+[ \t]+){1,5}?"
            r"(?:street|avenue|road|boulevard|lane|drive|court|place|walk|st|ave|rd|blvd|ln|dr|ct)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "ADDRESS",
        re.compile(
            rf"(?<![\w-])(?:{HOUSE_NUMBER}[ \t]+{ROMANCE_STREET_NAME}"
            rf"|(?:{ROMANCE_STREET_NAME}|{GERMAN_STREET})[ \t]+{HOUSE_NUMBER})(?![\w-])"
        ),
    ),
    ("POSTCODE", re.compile(rf"(?<![\w-])(?:{POSTCODE})(?=[ \t]+{NAME_WORD}\b)")),
    (
        "PHONE",
        re.compile(
            r"(?<![\w+])(?:\(\d{3}\)|\d{3})[ .-]\d{3}[ .-]\d{4}(?!\d)"
            r"|(?<![\w+.-])(?:0\d(?:[ .-]\d{2}){4}|\d{3}[ .-]\d{3}[ .-]\d{3})(?![ .-]?\d)"
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
    re.compile(
        r"\b(?:forwarded|quoted|attached|linked|retrieved)\b.{0,80}"
        r"\b(?:note|message|document|page|policy|instructions?)\b.{0,80}"
        r"\b(?:tells?|requires?|instructs?|directs?)\b.{0,50}"
        r"\b(?:assistant|model|agent|system)\b"
    ),
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


def normalize_text(text: str) -> str:
    return "".join(
        char for char in unicodedata.normalize("NFKC", text) if unicodedata.category(char) != "Cf"
    )


def redact_name(match: re.Match[str]) -> str:
    name = match["name"]
    folded = name.casefold()
    if "explicit" not in match.re.groupindex and (
        folded in NON_PERSON_TERMS or PRODUCT_TERMS.intersection(folded.split())
    ):
        return match[0]
    return match[0][: match.start("name") - match.start()] + "[NAME]"


def redact_text(
    text: str,
    *,
    known_names: Sequence[str] = (),
    sender_name: str = "",
    sender_address: str = "",
) -> str:
    text = normalize_text(text)
    for label, pattern in PII_PATTERNS:
        text = pattern.sub(f"[{label}]", text)
    names = sorted(
        {normalize_text(value).strip() for value in known_names} - {""},
        key=len,
        reverse=True,
    )
    for label, values in (("NAME", names), ("SENDER", (sender_address, sender_name))):
        for value in values:
            known_value = normalize_text(value).strip()
            if len(known_value) >= 2:
                text = re.sub(
                    rf"(?<![\w\[]){re.escape(known_value)}(?![\w\]])",
                    f"[{label}]",
                    text,
                    flags=re.IGNORECASE,
                )
    for pattern in NAME_PATTERNS:
        text = pattern.sub(redact_name, text)
    return text


def detect_prompt_injection(text: str) -> bool:
    compact = " ".join(normalize_text(text).casefold().split())
    compact = re.sub(r"\bi[ ._-]+g[ ._-]+n[ ._-]+o[ ._-]+r[ ._-]+e\b", "ignore", compact)
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
