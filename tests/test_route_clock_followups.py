import asyncio
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path

import pytest

import verdict.api as api
import verdict.relay as relay
from tests.helpers.live_execution_path import install_live_fakes, live_service, public_request_body
from verdict.intelligence import IntelligenceService


def _service() -> IntelligenceService:
    return IntelligenceService(
        primary_model="gpt-4o",
        providers={},
        profile="development",
        log_path="tests/test.log",
        log_full_task=False,
        discovery_ttl=3600,
    )


def test_admission_proof_uses_injected_admit_now(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, no_gateway_network: None
) -> None:
    install_live_fakes(monkeypatch, tmp_path)

    seen: set[datetime] = set()
    original_admission_proof = relay.admission_proof

    def spy(*args, **kwargs):
        if "now" in kwargs:
            seen.add(kwargs["now"])
        return original_admission_proof(*args, **kwargs)

    monkeypatch.setattr(relay, "admission_proof", spy)

    injected = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(seconds=5)
    body = public_request_body()
    context = api._authority_context(dict(body), task=body["task"])
    context["_dispatch_gateway_origin"] = "http://127.0.0.1:20128"

    decision = asyncio.run(
        live_service(admit_now=injected).route(body["task"], criticality="low", context=context)
    )

    assert decision.decision == "selected"
    assert seen and set(seen) == {injected}


def test_admit_now_with_offsetless_tzinfo_is_rejected() -> None:
    class _NoOffset(tzinfo):
        def utcoffset(self, dt):
            return None

        def dst(self, dt):
            return None

        def tzname(self, dt):
            return None

    value = datetime(2026, 11, 17, 12, 0, tzinfo=_NoOffset())
    svc = _service()
    with pytest.raises(ValueError, match="timezone-aware"):
        svc.admit_now = value
    assert svc.admit_now is None
