from pathlib import Path
from runpy import run_path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "demo_report"


@pytest.fixture
def cases() -> dict[str, list[dict[str, object]]]:
    scene_evidence = {
        "verified": [
            {"event_kind": "policy_decision", "rule_id": "ORDER_STATUS", "decision": "auto"},
            {"event_kind": "draft", "status": "sent", "draft_body": "Your order has shipped."},
        ],
        "unknown": [
            {
                "event_kind": "lookup",
                "tool_key": "order_identity_check",
                "result_reason": "identity_unverified",
            }
        ],
        "refund": [
            {"event_kind": "approval", "required_role": "manager", "decision": "pending"},
            {"event_kind": "approval", "required_role": "manager", "decision": "approved"},
            {
                "event_kind": "action",
                "risk_level": "money",
                "status": "executed",
                "amount": 210,
                "currency": "EUR",
            },
        ],
        "expired": [
            {
                "event_kind": "lookup",
                "tool_key": "order_refund_eligibility",
                "result_reason": "window_expired",
            }
        ],
        "refunded": [
            {
                "event_kind": "lookup",
                "tool_key": "order_refund_eligibility",
                "result_reason": "already_refunded",
            }
        ],
        "injection": [{"event_kind": "policy_decision", "rule_id": "G1", "decision": "escalate"}],
    }
    return {
        scene: [
            {"created_at": "2026-11-15 00:00:00 UTC", **row}
            for row in [{"event_kind": "message", "body_redacted": "Help with my order."}, *rows]
        ]
        for scene, rows in scene_evidence.items()
    }


def test_demo_report_renders_matching_recording_and_escapes_text(
    cases: dict[str, list[dict[str, object]]],
) -> None:
    body = "<script>alert(1)</script> someone@example.test"
    cases["verified"][0].update(
        body_redacted=body, body_text="private raw body", sender_address="sender@example.test"
    )
    cases["verified"][2].update(draft_body=body, output={"private": "raw payload"})
    page = run_path(str(SCRIPT))["render_report"]("2026-11-15 00:00:02 UTC", cases, (1, 2))
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt; [EMAIL]" in page
    assert "@" not in page.replace("@media", "")
    for private in ("body_text", "sender_address", "private raw body", "raw payload"):
        assert private not in page
    assert page.count('<section class="screen') == 9
    assert page.count("<details>") == 7
    assert "Refunded after approval" in page


@pytest.mark.parametrize(
    ("scene", "row", "replacement"),
    [
        ("verified", 2, {"status": "rejected"}),
        ("unknown", None, {"event_kind": "lookup", "tool_key": "order_lookup"}),
        ("refund", 3, {"amount": 211}),
        ("expired", None, {"event_kind": "action", "risk_level": "money"}),
        ("refunded", 1, {"result_reason": "eligible"}),
        ("injection", None, {"event_kind": "action", "risk_level": "external_comm"}),
    ],
)
def test_demo_report_rejects_mismatching_scene(
    cases: dict[str, list[dict[str, object]]],
    scene: str,
    row: int | None,
    replacement: dict[str, object],
) -> None:
    if row is None:
        cases[scene].append(replacement)
    else:
        cases[scene][row].update(replacement)
    with pytest.raises(ValueError, match=rf"Scene '{scene}' does not match the recording"):
        run_path(str(SCRIPT))["render_report"]("2026-11-15 00:00:02 UTC", cases, (1, 2))
