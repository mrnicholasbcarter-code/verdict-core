#!/usr/bin/env python3
"""
BOD-238 offline demo (NOT live evidence).

Demonstrates ADVISORY mode with a FakeProvider and a static catalog.
This is NOT the live check required by the acceptance gate.

Live check (item 1): will be run after BOD-235 (#621) merges and
factory.provider_from_env() is available. At that point, run with the
operator's real key via `zsh -ic` and the real Codiv API.

3 route calls:
  1. trivial  (economy signals -> cheap pick)
  2. hard     (strength signals -> strong pick)
  3. protected (critical -> advisory skipped, protected=True)
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from typing import Any

os.environ["VERDICT_DECISION_SIGNALS_MODE"] = "ADVISORY"

from verdict.decision_signals.advisory import _mode_from_env
from verdict.decision_signals.contracts import DecisionSignalSetV1
from verdict.intelligence import IntelligenceService
from verdict.models import ProviderConfig

_DIGEST = "a" * 64


def _make_signals(fw: float = 0.5, cx: float = 0.5, confidence: float = 0.9) -> DecisionSignalSetV1:
    return DecisionSignalSetV1(
        schema_version="decision-signals/v1",
        provider="fake-provider",
        model="fake-model",
        version="0.1",
        request_id="evidence-run",
        purpose="route",
        signals={
            "complexity": cx,
            "decomposability": 0.5,
            "ambiguity": 0.3,
            "frontier_worthy": fw,
            "security_sensitive": 0.1,
            "verification_strength": 0.5,
            "context_need": 0.4,
        },
        confidence=confidence,
        latency_ms=5,
        usage={"input_tokens": 10, "output_tokens": 5},
        input_digest=_DIGEST,
        observed_at=datetime.now(timezone.utc).isoformat(),
        failure_class=None,
        mode="ADVISORY",
    )


CALLS = [
    # (label, task_str, criticality, fw, cx, expected_profile)
    ("trivial", "write hello world", "low", 0.15, 0.15, "economy"),
    ("hard", "architect distributed consensus", "high", 0.85, 0.80, "strength"),
    ("protected", "critical security audit", "critical", 0.15, 0.15, "NOT_CALLED"),
]


class FakeProvider:
    def __init__(self, fw: float, cx: float) -> None:
        self.fw = fw
        self.cx = cx
        self.called = False

    def signals(self, question: Any, *, now: Any) -> DecisionSignalSetV1:
        self.called = True
        return _make_signals(fw=self.fw, cx=self.cx)


def _make_svc(provider: FakeProvider) -> IntelligenceService:
    models = {
        "gpt-4o": type(
            "MC", (), {"capabilities": [], "max_tokens": 8192, "cost_per_1k": 0.005, "pricing": {}}
        )(),
        "gpt-3.5-turbo": type(
            "MC", (), {"capabilities": [], "max_tokens": 4096, "cost_per_1k": 0.0005, "pricing": {}}
        )(),
    }
    providers = {"openai": ProviderConfig(api_key="fake-key", models=models, priority=1)}

    # Patch classify so gpt-4o -> tier 1, gpt-3.5-turbo -> tier 3
    import verdict.intelligence as _vi

    orig = _vi.classify
    _vi.classify = lambda mid: 1 if "4o" in mid else 3

    svc = IntelligenceService(
        primary_model="gpt-4o",
        providers=providers,
        profile="development",
        log_path="",
        log_full_task=False,
        discovery_ttl=60,
        allow_offline=True,
        decision_signal_provider=provider,
    )
    # Restore
    _vi.classify = orig
    return svc


async def main() -> None:
    print(f"Mode from env: {_mode_from_env()}")
    print()

    results = []
    for label, task, crit, fw, cx, expected_profile in CALLS:
        provider = FakeProvider(fw=fw, cx=cx)

        import verdict.intelligence as _vi

        orig = _vi.classify
        _vi.classify = lambda mid: 1 if "4o" in mid else 3

        svc = IntelligenceService(
            primary_model="gpt-4o",
            providers={
                "openai": ProviderConfig(
                    api_key="fake-key",
                    models={
                        "gpt-4o": type(
                            "MC",
                            (),
                            {
                                "capabilities": [],
                                "max_tokens": 8192,
                                "cost_per_1k": 0.005,
                                "pricing": {},
                            },
                        )(),
                        "gpt-3.5-turbo": type(
                            "MC",
                            (),
                            {
                                "capabilities": [],
                                "max_tokens": 4096,
                                "cost_per_1k": 0.0005,
                                "pricing": {},
                            },
                        )(),
                    },
                    priority=1,
                )
            },
            profile="development",
            log_path="",
            log_full_task=False,
            discovery_ttl=60,
            allow_offline=True,
            decision_signal_provider=provider,
        )
        _vi.classify = orig

        dec = await svc.route(task, criticality=crit)

        advisory_flags = [f for f in (dec.safety_flags or []) if "advisory" in f]
        result = {
            "call": label,
            "task": task,
            "criticality": crit,
            "expected_profile": expected_profile,
            "selected_model": dec.model,
            "protected": dec.protected,
            "provider_called": provider.called,
            "advisory_flags": advisory_flags,
            "decision": dec.decision,
        }
        results.append(result)

        print(f"=== Call: {label} ===")
        print(f"  task:             {task}")
        print(f"  criticality:      {crit}")
        print(f"  signals:          fw={fw} cx={cx}")
        print(f"  expected_profile: {expected_profile}")
        print(f"  selected_model:   {dec.model}")
        print(f"  protected:        {dec.protected}")
        print(f"  provider_called:  {provider.called}")
        print(f"  advisory_flags:   {advisory_flags}")
        print()

    # Verdicts
    all_ok = True
    print("=== Verdicts ===")

    # 1. trivial -> economy -> cheap model
    r = results[0]
    ok1 = "advisory:economy" in r["advisory_flags"] and "3.5" in r["selected_model"]
    print(
        f"[{'PASS' if ok1 else 'FAIL'}] trivial: economy profile, cheap model selected: {r['selected_model']}"
    )
    all_ok = all_ok and ok1

    # 2. hard -> strength -> gpt-4o
    r = results[1]
    ok2 = "advisory:strength" in r["advisory_flags"] and "4o" in r["selected_model"]
    print(
        f"[{'PASS' if ok2 else 'FAIL'}] hard: strength profile, strong model selected: {r['selected_model']}"
    )
    all_ok = all_ok and ok2

    # 3. protected -> advisory not called
    r = results[2]
    ok3 = r["protected"] is True and not r["provider_called"]
    print(
        f"[{'PASS' if ok3 else 'FAIL'}] protected: advisory not called (protected={r['protected']}, provider_called={r['provider_called']})"
    )
    all_ok = all_ok and ok3

    print()
    print(f"RESULT: {'PASS' if all_ok else 'FAIL'}")


if __name__ == "__main__":
    asyncio.run(main())
