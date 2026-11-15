import json
from collections import Counter, defaultdict
from pathlib import Path

from app.guardrails import redact_text

LABELS = {
    "name": "[NAME]",
    "street": "[ADDRESS]",
    "postcode": "[POSTCODE]",
    "phone": "[PHONE]",
    "email": "[EMAIL]",
    "card": "[CARD]",
}


def print_scores(
    title: str, recall: dict[str, Counter[str]], precision: dict[str, Counter[str]]
) -> None:
    def score(counts: Counter[str]) -> str:
        passed, total = counts["passed"], counts["total"]
        return f"{passed}/{total} ({passed / total:.1%})" if total else "-"

    print(f"\n{title}")
    print("Group | Exact-span matches | Negative preservation")
    for group in sorted(recall.keys() | precision.keys()):
        print(f"{group} | {score(recall[group])} | {score(precision[group])}")


def test_pii_probes() -> None:
    path = Path(__file__).parent / "fixtures/pii_probes.jsonl"
    recall: dict[str, Counter[str]] = defaultdict(Counter)
    precision: dict[str, Counter[str]] = defaultdict(Counter)
    held_out_recall: dict[str, Counter[str]] = defaultdict(Counter)
    held_out_precision: dict[str, Counter[str]] = defaultdict(Counter)
    for line in path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        text, language, kind, spans = (row[key] for key in ("text", "language", "kind", "spans"))
        groups = ("all", language, kind, f"{language}/{kind}")
        held_out = row.get("held_out", False)
        redacted = redact_text(text)
        if spans:
            # Keep one exact span per positive row so partial and overlong masks both fail.
            assert set(spans) == {kind} and len(spans[kind]) == 1
            span = spans[kind][0]
            assert text.count(span) == 1
            target = held_out_recall if held_out else recall
            if not held_out and "\n" in text:
                groups += ("multiline",)
            matched = redacted == text.replace(span, LABELS[kind], 1)
        else:
            target = held_out_precision if held_out else precision
            matched = redacted == text
        for group in groups:
            target[group]["total"] += 1
            target[group]["passed"] += matched

    print_scores("Author-written probe set", recall, precision)
    print_scores("Held-out probe set (informational)", held_out_recall, held_out_precision)
    assert recall["all"]["total"] + precision["all"]["total"] >= 80
    assert precision["all"]["total"] >= 30
    for kind, minimum in {
        "name": 0.90,
        "street": 0.95,
        "email": 0.95,
        "phone": 0.95,
        "card": 0.95,
    }.items():
        assert recall[kind]["passed"] / recall[kind]["total"] >= minimum, kind
    assert precision["all"]["passed"] == precision["all"]["total"]
    assert recall["multiline"]["passed"] == recall["multiline"]["total"]
