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
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

EVIDENCE_PATH = (
    Path.home() / ".verdict" / "evidence" / "bod191" / "bod238" / "advisory-live-20260925.json"
)

TASKS = [
    {"label": "trivial", "task": "Write a hello world function in Python.", "criticality": "low"},
    {
        "label": "hard",
        "task": (
            "Design a distributed consensus protocol for a multi-region database "
            "with Byzantine fault tolerance."
        ),
        "criticality": "high",
    },
    {
        "label": "protected",
        "task": "Critical security audit of production payment processing system.",
        "criticality": "critical",
    },
]


def _check_env() -> None:
    mode = os.environ.get("VERDICT_DECISION_SIGNALS_MODE")
    assert mode == "ADVISORY", f"Must run with VERDICT_DECISION_SIGNALS_MODE=ADVISORY, got {mode!r}"


def _signals_digest(signals: dict[str, float] | None) -> str | None:
    """sha256 of the canonical (sorted-keys) JSON of the signals dict."""
    if signals is None:
        return None
    canonical = json.dumps(signals, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _make_capturing_provider(wrapped: Any) -> tuple[Any, list[Any], list[int]]:
    """Return (provider, signal_captures, call_count) where signal_captures
    collects the DecisionSignalSetV1 objects returned by the real provider."""
    signal_captures: list[Any] = []
    call_count: list[int] = [0]

    class _CapturingProvider:
        def signals(self, question: Any, *, now: Any) -> Any:
            call_count[0] += 1
            result = wrapped.signals(question, now=now)
            signal_captures.append(result)
            return result

    return _CapturingProvider(), signal_captures, call_count


def _make_svc(provider: Any) -> Any:
    import verdict.intelligence as _vi
    from verdict.intelligence import IntelligenceService
    from verdict.models import ProviderConfig

    models = {
        "gpt-4o": type(
            "MC", (), {"capabilities": [], "max_tokens": 8192, "cost_per_1k": 0.005, "pricing": {}}
        )(),
        "gpt-3.5-turbo": type(
            "MC", (), {"capabilities": [], "max_tokens": 4096, "cost_per_1k": 0.0005, "pricing": {}}
        )(),
    }
    providers = {
        "openai": ProviderConfig(
            api_key="sk-fake-key-for-static-catalog", models=models, priority=1
        )
    }
    orig_classify = _vi.classify
    _vi.classify = lambda mid: 1 if "4o" in mid else 3  # type: ignore[assignment]
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


async def _route_with_influence(svc: Any, task_str: str, criticality: str) -> tuple[Any, Any]:
    """Run svc.route() and also intercept the InfluenceRecord from advise_order().

    Returns (RoutingDecision, InfluenceRecord | None).
    """
    import verdict.decision_signals.advisory as _adv
    import verdict.intelligence as _vi

    influence_capture: list[Any] = []
    orig_advise = _adv.advise_order

    def _capturing_advise_order(candidates: Any, signals: Any, **kwargs: Any) -> tuple[Any, Any]:
        result = orig_advise(candidates, signals, **kwargs)
        influence_capture.append(result[1])  # InfluenceRecord is item [1]
        return result

    _adv.advise_order = _capturing_advise_order  # type: ignore[assignment]
    orig_classify = _vi.classify
    _vi.classify = lambda mid: 1 if "4o" in mid else 3  # type: ignore[assignment]
    try:
        dec = await svc.route(task_str, criticality=criticality)
    finally:
        _adv.advise_order = orig_advise
        _vi.classify = orig_classify

    influence = influence_capture[0] if influence_capture else None
    return dec, influence


async def main() -> None:
    _check_env()

    from verdict.decision_signals.factory import provider_from_env

    base_provider = provider_from_env()
    if base_provider is None:
        print(
            "ERROR: provider_from_env() returned None. Check TYPESAFE_API_KEY and mode.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"Provider: {type(base_provider).__name__}", file=sys.stderr)
    print(f"Mode: {os.environ.get('VERDICT_DECISION_SIGNALS_MODE')}", file=sys.stderr)

    evidence_entries: list[dict[str, Any]] = []
    errors: list[str] = []

    for task_def in TASKS:
        label = task_def["label"]
        task_str = task_def["task"]
        crit = task_def["criticality"]
        is_protected = crit == "critical"

        capturing, signal_captures, call_count = _make_capturing_provider(base_provider)
        svc = _make_svc(capturing)

        dec, influence = await _route_with_influence(svc, task_str, crit)

        flags = list(dec.safety_flags or [])
        advisory_flags = [f for f in flags if "advisory" in f]

        # Extract signal-set fields from the captured real response
        sig_set = signal_captures[0] if signal_captures else None
        sig_request_id: str | None = getattr(sig_set, "request_id", None) if sig_set else None
        sig_model: str | None = getattr(sig_set, "model", None) if sig_set else None
        sig_failure_class: str | None = None
        if sig_set is not None:
            fc = getattr(sig_set, "failure_class", None)
            sig_failure_class = fc.value if fc is not None else None
        sig_confidence: float | None = getattr(sig_set, "confidence", None) if sig_set else None
        raw_signals: dict[str, float] | None = (
            getattr(sig_set, "signals", None) if sig_set else None
        )
        sig_signals_rounded: dict[str, float] | None = (
            {k: round(v, 3) for k, v in raw_signals.items()} if raw_signals else None
        )
        computed_signals_digest = _signals_digest(raw_signals)

        # InfluenceRecord baseline_first and advised_first
        baseline_first: str | None = (
            getattr(influence, "baseline_first", None) if influence else None
        )
        advised_first: str | None = getattr(influence, "advised_first", None) if influence else None
        inf_profile: str | None = getattr(influence, "profile", None) if influence else None

        # Validate non-protected tasks
        if not is_protected:
            if sig_failure_class is not None:
                errors.append(f"[{label}] FAIL: failure_class={sig_failure_class!r}, expected null")
            if not sig_request_id:
                errors.append(f"[{label}] FAIL: no request_id returned from provider")

        entry: dict[str, Any] = {
            "label": label,
            "task_summary": task_str[:80],
            "criticality": crit,
            # routing decision
            "model": dec.model,
            "routing_provider": dec.provider,
            "tier": dec.tier,
            "protected": dec.protected,
            "provider_called": call_count[0] > 0,
            # from real DecisionSignalSetV1
            "request_id": sig_request_id,
            "response_model": sig_model,
            "failure_class": sig_failure_class,
            "confidence": sig_confidence,
            "signals": sig_signals_rounded,
            "signals_digest": computed_signals_digest,
            # advisory influence
            "advisory_profile": inf_profile,
            "baseline_first": baseline_first,
            "advised_first": advised_first,
            "advisory_flags": advisory_flags,
        }

        print(
            f"[{label}] model={dec.model} protected={dec.protected} "
            f"provider_called={entry['provider_called']} "
            f"request_id={'<present>' if sig_request_id else 'NONE'} "
            f"response_model={sig_model} failure_class={sig_failure_class} "
            f"confidence={sig_confidence} "
            f"baseline={baseline_first} advised={advised_first}",
            file=sys.stderr,
        )

        evidence_entries.append(entry)

    if errors:
        for e in errors:
            print(e, file=sys.stderr)
        sys.exit(1)

    # Save evidence (chmod 600; no keys)
    EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    evidence = {
        "schema": "advisory-live-evidence/v2",
        "mode": os.environ.get("VERDICT_DECISION_SIGNALS_MODE"),
        "provider_type": type(base_provider).__name__,
        "timeout_ms": int(os.environ.get("VERDICT_DECISION_SIGNALS_TIMEOUT_MS", "5000")),
        "tasks": evidence_entries,
    }
    evidence_json = json.dumps(evidence, indent=2)
    EVIDENCE_PATH.write_text(evidence_json)
    EVIDENCE_PATH.chmod(0o600)

    print(f"Evidence saved to {EVIDENCE_PATH} (chmod 600)", file=sys.stderr)
    print(evidence_json)
    print("PASS", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
