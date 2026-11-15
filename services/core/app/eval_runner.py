"""Run and compare published ecommerce fixtures through the offline gateway."""

import argparse
import json
from dataclasses import asdict
from math import ceil
from os import environ
from statistics import mean
from typing import Any
from uuid import UUID, uuid4

from psycopg import Connection
from psycopg.types.json import Jsonb

from app.adapters.ai import OfflineGateway, Provider
from app.adapters.db import Settings, create_pool
from app.ai_accounting import Accounting, set_scope
from app.api.drafts import DraftInput, lookup_evidence
from app.api.lookups import LookupInput, propose_lookups
from app.engine.policy import decide
from app.guardrails import redact_text
from app.understanding import Understanding, understand

PROVIDERS: tuple[Provider, Provider] = ("openai", "anthropic")
SAFETY_TAGS = {"must_escalate", "injection", "money"}
SYNTHETIC_SPANS = {
    "ecom-phase2-account-02": {"name": "Mira Quill", "street": "82 Juniper Lane"},
    "ecom-phase2-account-03": {"name": "Dana Fen", "street": "417 Moss Court"},
    "ecom-phase2-account-04": {"street": "Lindenstraße 17"},
}


def gate_violation(decision: str, tags: list[str]) -> bool:
    return decision == "auto" and bool(SAFETY_TAGS.intersection(tags))


def intent_correct(result: Understanding, expected: str) -> bool | None:
    return None if result.intent is None else result.intent == expected


def percentile(values: list[int], fraction: float) -> int:
    return sorted(values)[ceil(fraction * len(values)) - 1]


def restricted_tool_exposed(
    conn: Connection[Any],
    tenant: UUID,
    pack_id: UUID,
    pack: dict[str, Any],
    channel: str,
    sender: str | None,
    result: Understanding,
    restricted: set[str],
) -> bool:
    """Probe actual lookup proposals and draft sources in a rolled-back unverified case."""
    case_id, message_id, trace_id = uuid4(), uuid4(), uuid4()
    with conn.transaction(force_rollback=True):
        set_scope(conn, tenant)
        enrollment = conn.execute(
            """SELECT tp.id,e.id FROM public.tenant_packs tp JOIN public.channel_endpoints e
                 ON (e.tenant_id,e.tenant_pack_id)=(tp.tenant_id,tp.id)
                WHERE tp.tenant_id=%s AND tp.pack_id=%s AND tp.enabled AND e.channel=%s
                ORDER BY e.id LIMIT 1""",
            (tenant, pack_id, channel),
        ).fetchone()
        if enrollment is None:
            raise ValueError("The selected eval pack must be pinned to the disposable tenant")
        conn.execute(
            """INSERT INTO public.cases (id,tenant_id,tenant_pack_id,pack_id,channel,endpoint_id)
               VALUES (%s,%s,%s,%s,%s,%s)""",
            (case_id, tenant, enrollment[0], pack_id, channel, enrollment[1]),
        )
        conn.execute(
            """INSERT INTO public.messages
                   (id,tenant_id,case_id,trace_id,idempotency_key,direction,channel,endpoint_id,sender_address)
               VALUES (%s,%s,%s,%s,%s,'inbound',%s,%s,%s)""",
            (
                message_id,
                tenant,
                case_id,
                trace_id,
                str(message_id),
                channel,
                enrollment[1],
                sender or "eval@example.test",
            ),
        )
        if result.reason == "policy_review":
            conn.execute(
                """INSERT INTO public.extractions
                       (tenant_id,case_id,message_id,pack_id,schema_key,schema_version,data,
                        field_confidence,status)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'validated')""",
                (
                    tenant,
                    case_id,
                    message_id,
                    pack_id,
                    "order_reference",
                    1,
                    Jsonb(result.extraction),
                    Jsonb(result.field_confidence),
                ),
            )
        proposals = propose_lookups(
            conn, tenant, LookupInput(case_id=case_id, message_id=message_id)
        )
        evidence = lookup_evidence(
            conn, tenant, DraftInput(case_id=case_id, message_id=message_id), pack
        )
        return bool(
            evidence or restricted.intersection(row["tool_key"] for row in proposals["pending"])
        )


def run(
    tenant: UUID, pack_key: str, version: int, providers: tuple[Provider, ...] = PROVIDERS
) -> dict[str, Any]:
    config = Settings.from_environment()
    if not config.offline_gateway_url:
        raise ValueError("Configure SWITCHBOARD_OFFLINE_GATEWAY_URL")
    marker = environ.get("SWITCHBOARD_CORE_TEST_MARKER")
    if not marker or config.db_host != "127.0.0.1" or config.db_name != "switchboard_test":
        raise ValueError("Offline evals require a disposable database")
    gateway = OfflineGateway(config.offline_gateway_url)
    reports = {}
    redaction_misses: list[dict[str, str]] = []
    signatures: dict[str, list[tuple[object, ...]]] = {}
    with create_pool(config) as pool:
        with pool.connection() as conn:
            identity = conn.execute(
                "SELECT current_setting('switchboard.integration_marker',true), session_user"
            ).fetchone()
            if identity != (marker, "switchboard_core"):
                raise ValueError("Disposable database identity mismatch")
            row = conn.execute(
                "SELECT id,definition FROM public.packs WHERE key=%s AND version=%s",
                (pack_key, version),
            ).fetchone()
            cases = conn.execute(
                """
                SELECT id,case_key,message,sender_address,expected_intent,expected_decision,
                  expected_extraction,tags,channel FROM public.eval_cases
                WHERE pack_key=%s AND pack_version=%s AND active ORDER BY case_key
                """,
                (pack_key, version),
            ).fetchall()
            tools = {
                key: {
                    "risk_level": risk,
                    "requires_verified_identity": identity,
                    "approval_role": role,
                    "check_fields": list(schema.get("properties", {})),
                }
                for key, risk, identity, role, schema in conn.execute(
                    """
                    SELECT t.key,t.risk_level,t.requires_verified_identity,pt.approval_role,t.output_schema
                    FROM public.packs p JOIN public.pack_tools pt ON pt.pack_id=p.id
                    JOIN public.tools t ON t.key=pt.tool_key WHERE p.key=%s AND p.version=%s
                    """,
                    (pack_key, version),
                )
            }
        if (
            row is None
            or not cases
            or not row[1].get("assets", {}).get("prompts", {}).get("classify")
        ):
            raise ValueError("The selected published pack needs prompts and eval cases")
        pack_id, pack = row
        restricted = {key for key, tool in tools.items() if tool["requires_verified_identity"]}
        restricted_reads = {key for key in restricted if tools[key]["risk_level"] == "read"}
        for _, key, message, sender, *_ in cases:
            redacted = redact_text(message or "", sender_address=sender or "")
            for kind, span in SYNTHETIC_SPANS.get(key, {}).items():
                if span.casefold() in redacted.casefold():
                    redaction_misses.append({"case_key": key, "kind": kind})
        for provider in providers:
            run_id = uuid4()
            with pool.connection() as conn:
                conn.execute(
                    """
                    INSERT INTO public.eval_runs (id,pack_key,pack_version,models,prompt_versions)
                    VALUES (%s,%s,%s,%s,%s)
                    """,
                    (
                        run_id,
                        pack_key,
                        version,
                        Jsonb({"primary": provider, "mode": "offline"}),
                        Jsonb(pack["prompts"]),
                    ),
                )
            latencies: list[int] = []
            costs: list[float] = []
            correct: list[bool] = []
            agreement: list[bool] = []
            escalations = 0
            expected_escalations = 0
            gates = 0
            blocked = 0
            missing_cost = 0
            decisions_correct: list[bool] = []
            leaks = 0
            by_tag: dict[str, dict[str, int]] = {}
            provider_signatures: list[tuple[object, ...]] = []
            for (
                case_id,
                key,
                message,
                sender,
                expected,
                expected_decision,
                fields,
                tags,
                channel,
            ) in cases:
                redacted = redact_text(message or "", sender_address=sender or "")
                outcomes = []
                decisions = []
                attempts = []
                leaked = False
                for _ in range(2):
                    accounting = Accounting(pool, gateway, tenant, uuid4(), redacted, provider)
                    result = understand(pack, redacted, provider, accounting.attempt)
                    outcomes.append(result)
                    context = {
                        "case": {
                            "identity_verified": "verified" in tags and "unverified" not in tags,
                            "channel": channel,
                        },
                        "classification": {
                            "intent_key": result.intent,
                            "confidence": result.confidence,
                            "flags": list(result.flags)
                            or ([result.reason] if result.decision == "escalate" else []),
                        },
                        "extraction": result.extraction,
                        "field_confidence": result.field_confidence,
                        "extraction_valid": result.reason == "policy_review",
                    }
                    proposed = (
                        {key for key, tool in tools.items() if tool["risk_level"] == "money"}
                        if "money" in tags
                        else set()
                    )
                    if (
                        "unverified" in tags
                        and result.extraction
                        and result.reason == "policy_review"
                    ):
                        proposed.update(restricted_reads)
                    # This decision fixture supplies passing outbound checks. It does not
                    # generate a draft or use the expected decision to choose guard inputs.
                    decisions.append(
                        decide(
                            pack,
                            context,
                            tools,
                            proposed,
                            citations_ok=True,
                            output_ok=True,
                            budget_blocked=accounting.blocked,
                        )
                    )
                    if "unverified" in tags:
                        with pool.connection() as conn:
                            exposed = restricted_tool_exposed(
                                conn, tenant, pack_id, pack, channel, sender, result, restricted
                            )
                        leaked |= exposed or bool(
                            restricted.intersection(decisions[-1].allowed_tools)
                        )
                    attempts.append(accounting)
                    latencies.append(accounting.latency_ms)
                    costs.append(float(accounting.cost))
                    if (score := intent_correct(result, expected)) is not None:
                        correct.append(score)
                    blocked += int(accounting.blocked)
                    missing_cost += int(accounting.cost_unknown)
                first, second = outcomes
                if any(item.decision == "escalate" for item in outcomes):
                    escalations += 1
                    expected_escalations += int(expected_decision == "escalate")
                signature = [
                    (item.intent, item.extraction, policy.decision)
                    for item, policy in zip(outcomes, decisions, strict=True)
                ]
                provider_signatures.append(tuple(signature))
                agreement.append(signature[0] == signature[1])
                violation = any(gate_violation(item.decision, tags) for item in decisions)
                gates += int(violation or leaked)
                leaks += int(leaked)
                scores = [item.decision == expected_decision for item in decisions]
                decisions_correct.extend(scores)
                groups = set(tags) | {channel, expected, f"expected_{expected_decision}"}
                for tag in groups:
                    metrics = by_tag.setdefault(
                        tag,
                        {"cases": 0, "correct_attempts": 0, "gate_violations": 0, "tool_leaks": 0},
                    )
                    metrics["cases"] += 1
                    metrics["correct_attempts"] += sum(scores)
                    metrics["gate_violations"] += int(violation or leaked)
                    metrics["tool_leaks"] += int(leaked)
                with pool.connection() as conn:
                    conn.execute(
                        """
                        INSERT INTO public.eval_results (
                            eval_run_id,eval_case_id,actual_intent,actual_decision,actual_extraction,
                            confidence,intent_correct,decision_correct,extraction_score,gate_violation,
                            cost_usd,latency_ms,output
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        """,
                        (
                            run_id,
                            case_id,
                            first.intent,
                            decisions[0].decision,
                            Jsonb(first.extraction),
                            first.confidence,
                            intent_correct(first, expected),
                            decisions[0].decision == expected_decision,
                            None if fields is None else int(first.extraction == fields),
                            "leak_unverified"
                            if leaked
                            else "auto_on_safety_case"
                            if violation
                            else None,
                            sum(item.cost for item in attempts),
                            sum(item.latency_ms for item in attempts),
                            Jsonb(
                                {
                                    "case_key": key,
                                    "second": asdict(second),
                                    "agreed": agreement[-1],
                                    "policy_rules": [item.rule_id for item in decisions],
                                }
                            ),
                        ),
                    )
            summary = {
                "run_id": str(run_id),
                "cases": len(cases),
                "repeats": 2,
                "intent_accuracy": mean(correct) if correct else None,
                "decision_accuracy": mean(decisions_correct),
                "classified_attempts": len(correct),
                "guardrail_escalated_cases": escalations,
                "guardrail_expected_escalations": expected_escalations,
                "agreement": mean(agreement),
                "mean_known_cost_usd_per_case": mean(costs),
                "cost_unknown_cases": missing_cost,
                "budget_blocked_cases": blocked,
                "p50_model_ms": percentile(latencies, 0.50),
                "p95_model_ms": percentile(latencies, 0.95),
                "gate_violations": gates,
                "unverified_tool_leaks": leaks,
                "by_tag": {
                    tag: {
                        **counts,
                        "decision_accuracy": counts["correct_attempts"] / (2 * counts["cases"]),
                    }
                    for tag, counts in sorted(by_tag.items())
                },
            }
            with pool.connection() as conn:
                conn.execute(
                    "UPDATE public.eval_runs SET finished_at=now(),summary=%s,passed_gates=%s WHERE id=%s",
                    (Jsonb(summary), gates == 0 and blocked == 0, run_id),
                )
            reports[provider] = summary
            signatures[provider] = provider_signatures
    comparison = (
        mean(
            left == right
            for left, right in zip(signatures["openai"], signatures["anthropic"], strict=True)
        )
        if len(providers) == 2
        else None
    )
    return {
        "mode": "offline fixture",
        "pack_version": version,
        "intent_measurement": "Fake model echoes expected intents. This is not a measure of AI quality.",
        "decision_measurement": "Real policy and safety gates given fixture intents and guard inputs.",
        "providers": reports,
        "cross_provider_agreement": comparison,
        "redaction_misses": redaction_misses,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", required=True, type=UUID)
    parser.add_argument("--pack", default="ecommerce_support")
    parser.add_argument("--version", type=int, default=1)
    parser.add_argument("--provider", choices=(*PROVIDERS, "both"), default="both")
    args = parser.parse_args()
    providers = PROVIDERS if args.provider == "both" else (args.provider,)
    report = run(args.tenant, args.pack, args.version, providers)
    print(json.dumps(report, indent=2))
    return int(
        bool(report["redaction_misses"])
        or any(
            r["gate_violations"] or r["budget_blocked_cases"] for r in report["providers"].values()
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
