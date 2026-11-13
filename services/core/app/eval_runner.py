"""Run and compare published ecommerce fixtures through the offline gateway."""

import argparse
import json
from dataclasses import asdict
from math import ceil
from os import environ
from statistics import mean
from typing import Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from app.adapters.ai import OfflineGateway, Provider
from app.adapters.db import Settings, create_pool
from app.ai_accounting import Accounting
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
                "SELECT definition FROM public.packs WHERE key=%s AND version=%s",
                (pack_key, version),
            ).fetchone()
            cases = conn.execute(
                """
                SELECT id,case_key,message,sender_address,expected_intent,expected_decision,
                  expected_extraction,tags FROM public.eval_cases
                WHERE pack_key=%s AND pack_version=%s AND active ORDER BY case_key
                """,
                (pack_key, version),
            ).fetchall()
        if (
            row is None
            or not cases
            or not row[0].get("assets", {}).get("prompts", {}).get("classify")
        ):
            raise ValueError("The selected published pack needs prompts and eval cases")
        pack = row[0]
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
            provider_signatures: list[tuple[object, ...]] = []
            for case_id, key, message, sender, expected, expected_decision, fields, tags in cases:
                redacted = redact_text(message or "", sender_address=sender or "")
                outcomes = []
                attempts = []
                for _ in range(2):
                    accounting = Accounting(pool, gateway, tenant, uuid4(), redacted, provider)
                    result = understand(pack, redacted, provider, accounting.attempt)
                    outcomes.append(result)
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
                signature = [(item.intent, item.extraction, item.decision) for item in outcomes]
                provider_signatures.append(tuple(signature))
                agreement.append(signature[0] == signature[1])
                violation = any(gate_violation(item.decision, tags) for item in outcomes)
                gates += int(violation)
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
                            first.decision,
                            Jsonb(first.extraction),
                            first.confidence,
                            intent_correct(first, expected),
                            first.decision == expected_decision,
                            None if fields is None else int(first.extraction == fields),
                            "auto_on_safety_case" if violation else None,
                            sum(item.cost for item in attempts),
                            sum(item.latency_ms for item in attempts),
                            Jsonb(
                                {"case_key": key, "second": asdict(second), "agreed": agreement[-1]}
                            ),
                        ),
                    )
            summary = {
                "run_id": str(run_id),
                "cases": len(cases),
                "repeats": 2,
                "intent_accuracy": mean(correct) if correct else None,
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
        "status": "built, not measured",
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
        any(r["gate_violations"] or r["budget_blocked_cases"] for r in report["providers"].values())
    )


if __name__ == "__main__":
    raise SystemExit(main())
