from typing import Any

from jsonschema import Draft202012Validator

from app.understanding import MODEL_RISK_FLAGS, understand

PACK: dict[str, Any] = {
    "intents": [
        {"key": "complaint", "default_priority": "high"},
        {"key": "other", "default_priority": "normal"},
    ],
    "prompts": {"classify": "prompts/classify.v1.md"},
    "assets": {"prompts": {"classify": "Classify the customer message."}},
    "thresholds": {"auto_confidence": 0.8, "field_confidence": 0.9},
}


def classify_with(flags: list[str]) -> Any:
    def attempt(
        step: str, provider: str, version: str, instructions: str, schema: dict[str, Any]
    ) -> dict[str, Any]:
        output = {"intent_key": "complaint", "confidence": 0.95, "risk_flags": flags}
        assert Draft202012Validator(schema).is_valid(output)
        return output

    return attempt


def test_classification_schema_requires_known_risk_flags() -> None:
    captured: dict[str, Any] = {}

    def attempt(
        step: str, provider: str, version: str, instructions: str, schema: dict[str, Any]
    ) -> None:
        captured.update(schema)

    understand(PACK, "Where is my parcel?", "openai", attempt)
    assert "risk_flags" in captured["required"]
    validator = Draft202012Validator(captured)
    assert validator.is_valid({"intent_key": "other", "confidence": 0.5, "risk_flags": []})
    assert not validator.is_valid(
        {"intent_key": "other", "confidence": 0.5, "risk_flags": ["approve_refund"]}
    )
    assert set(MODEL_RISK_FLAGS) == {"safety", "harassment", "legal_threat", "chargeback"}


def test_model_risk_flags_escalate_without_changing_the_reason() -> None:
    calm = understand(PACK, "The jacket arrived torn.", "openai", classify_with([]))
    assert (calm.decision, calm.reason, calm.flags) == ("needs_approval", "policy_review", ())

    flagged = understand(
        PACK, "The jacket arrived torn.", "openai", classify_with(["harassment", "harassment"])
    )
    assert flagged.decision == "escalate"
    assert flagged.reason == "policy_review"
    assert flagged.flags == ("harassment",)
    assert flagged.intent == "complaint"
