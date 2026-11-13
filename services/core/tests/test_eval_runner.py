from app.eval_runner import intent_correct
from app.understanding import Understanding


def test_intent_scoring_excludes_unclassified_attempts() -> None:
    skipped = Understanding(None, None, {}, "en", "escalate", "prompt_injection")
    classified = Understanding("account", 0.9, {}, "en", "needs_approval", "policy_review")
    assert intent_correct(skipped, "other") is None
    assert intent_correct(classified, "account") is True
    assert intent_correct(classified, "other") is False
