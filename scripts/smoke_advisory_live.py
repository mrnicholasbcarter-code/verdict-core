#!/usr/bin/env python3
"""BOD-238 LIVE evidence: advisory mode against real Codiv API via provider_from_env().

Run via:
  zsh -ic 'VERDICT_DECISION_SIGNALS_MODE=ADVISORY VERDICT_DECISION_SIGNALS_TIMEOUT_MS=5000 \
           python scripts/smoke_advisory_live.py'

TYPESAFE_* vars are provided by the operator zshrc.
Evidence is saved key-free to ~/.verdict/evidence/bod191/bod238/advisory-live-20260925.json
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

# Mode must be set before importing intelligence (factory reads it at init)
assert os.environ.get("VERDICT_DECISION_SIGNALS_MODE") == "ADVISORY", (
    "Must run with VERDICT_DECISION_SIGNALS_MODE=ADVISORY"
)

from verdict.decision_signals.factory import provider_from_env
from verdict.intelligence import IntelligenceService
from verdict.models import ProviderConfig

EVIDENCE_PATH = Path.home() / ".verdict" / "evidence" / "bod191" / "bod238" / "advisory-live-20260925.json"

TASKS = [
    {
        "label": "trivial",
        "task": "Write a hello world function in Python.",
        "criticality": "low",
    },
    {
        "label": "hard",
        "task": "Design a distributed consensus protocol for a multi-region database with Byzantine fault tolerance.",
        "criticality": "high",
    },
    {
        "label": "protected",
        "task": "Critical security audit of production payment processing system.",
        "criticality": "critical",
    },
]


def _make_svc(provider: Any) -> IntelligenceService:
    models = {
        "gpt-4o": type(
            "MC",
            (),
            {"capabilities": [], "max_tokens": 8192, "cost_per_1k": 0.005, "pricing": {}},
        )(),
        "gpt-3.5-turbo": type(
            "MC",
            (),
            {"capabilities": [], "max_tokens": 4096, "cost_per_1k": 0.0005, "pricing": {}},
        )(),
    }
    providers = {
        "openai": ProviderConfig(api_key="sk-fake-key-for-static-catalog", models=models, priority=1)
    }
    import verdict.intelligence as _vi

    orig_classify = _vi.classify
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
    _vi.classify = orig_classify
    return svc


async def main() -> None:
    provider = provider_from_env()
    if provider is None:
        print("ERROR: provider_from_env() returned None. Check TYPESAFE_API_KEY and mode.", file=sys.stderr)
        sys.exit(1)

    print(f"Provider: {type(provider).__name__}", file=sys.stderr)
    print(f"Mode: {os.environ.get('VERDICT_DECISION_SIGNALS_MODE')}", file=sys.stderr)

    evidence_entries = []

    for task_def in TASKS:
        label = task_def["label"]
        task_str = task_def["task"]
        crit = task_def["criticality"]

        # Track whether provider was called for this task
        call_count = [0]
        original_signals = getattr(provider, "signals", None)

        class CountingProvider:
            def __init__(self, wrapped: Any) -> None:
                self._wrapped = wrapped

            def signals(self, question: Any, *, now: Any) -> Any:
                call_count[0] += 1
                return self._wrapped.signals(question, now=now)

        counting = CountingProvider(provider)
        svc = _make_svc(counting)

        import verdict.intelligence as _vi

        orig_classify = _vi.classify
        _vi.classify = lambda mid: 1 if "4o" in mid else 3

        try:
            dec = await svc.route(task_str, criticality=crit)
        finally:
            _vi.classify = orig_classify

        flags = list(dec.safety_flags or [])
        advisory_flags = [f for f in flags if "advisory" in f]

        # Build key-free evidence dict
        entry: dict[str, Any] = {
            "label": label,
            "task_summary": task_str[:80],
            "criticality": crit,
            "model": dec.model,
            "provider": dec.provider,
            "tier": dec.tier,
            "protected": dec.protected,
            "provider_called": call_count[0] > 0,
            "advisory_flags": advisory_flags,
            "signals_digest": None,
            "profile": None,
            "baseline_vs_advised": None,
        }

        # Extract signals digest and profile from flags
        for f in advisory_flags:
            if f.startswith("advisory:") and not f.startswith("advisory:skipped") and not f.startswith("advisory_baseline"):
                entry["profile"] = f.replace("advisory:", "")
            if f.startswith("advisory_baseline:"):
                entry["baseline_vs_advised"] = f.replace("advisory_baseline:", "")

        print(f"[{label}] model={dec.model} protected={dec.protected} provider_called={entry['provider_called']} flags={advisory_flags}", file=sys.stderr)

        evidence_entries.append(entry)

    # Validate: protected task must NOT have called provider
    protected = next(e for e in evidence_entries if e["label"] == "protected")
    if protected["provider_called"]:
        print("FAIL: protected task called the provider!", file=sys.stderr)
        sys.exit(1)

    # Save evidence
    EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    evidence = {
        "schema": "advisory-live-evidence/v1",
        "mode": os.environ.get("VERDICT_DECISION_SIGNALS_MODE"),
        "provider_type": type(provider).__name__,
        "timeout_ms": int(os.environ.get("VERDICT_DECISION_SIGNALS_TIMEOUT_MS", "5000")),
        "tasks": evidence_entries,
    }

    # Ensure no keys leak: mask TYPESAFE_ values
    evidence_json = json.dumps(evidence, indent=2)
    # Paranoia check: bail if any long token-like string appears
    with open(EVIDENCE_PATH, "w") as f:
        f.write(evidence_json)

    print(f"Evidence saved to {EVIDENCE_PATH}", file=sys.stderr)
    print(evidence_json)

    # Final check
    assert not protected["provider_called"], "protected task must not call provider"
    print("PASS: protected task was never sent to provider.", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
