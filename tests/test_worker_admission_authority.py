from __future__ import annotations

from datetime import datetime, timezone

from verdict import availability, omniroute, worker_runtime
from verdict.availability import CandidateRequirements, OmniRouteAvailabilityAdapter, StaticOmniRouteTransport
from verdict.subagent_selection import WorkerTask

NOW = datetime(2026, 9, 26, 12, tzinfo=timezone.utc)


def _catalog(*model_ids: str) -> dict[str, object]:
    return {
        "data": [
            {
                "id": model_id,
                "provider": model_id.split("/", 1)[0],
                "context_length": 200_000,
                "capabilities": {"tool_calling": True, "reasoning": True},
                "pricing": {"input": 0, "output": 0},
            }
            for model_id in model_ids
        ]
    }


def _runtime(**rows: dict[str, object]) -> dict[str, dict[str, object]]:
    return {
        model_id: {
            "observed_at": NOW.isoformat(),
            "ttl_seconds": 60,
            "source": "fixture",
            "health": "healthy",
            **value,
        }
        for model_id, value in rows.items()
    }


def test_candidate_requirements_can_require_measured_usage_evidence() -> None:
    assert "require_usage_evidence" in CandidateRequirements.__dataclass_fields__

    report = OmniRouteAvailabilityAdapter(
        StaticOmniRouteTransport(
            _catalog("cc/no-usage", "kr/usable"),
            _runtime(
                **{
                    "cc/no-usage": {},
                    "kr/usable": {"quota_remaining_pct": 75.0},
                }
            ),
        )
    ).evaluate(CandidateRequirements(require_usage_evidence=True), now=NOW)

    assert [candidate.model.id for candidate in report.eligible] == ["kr/usable"]
    rejected = next(candidate for candidate in report.candidates if candidate.model.id == "cc/no-usage")
    explanation = next(
        row
        for row in availability.explain_candidates(
            list(report.candidates), CandidateRequirements(require_usage_evidence=True)
        )
        if row["model"] == rejected.model.id
    )
    assert explanation["rejected"] is True
    assert explanation["reason"] == "usage evidence unknown"


def test_zero_quota_is_never_admitted_even_if_health_is_healthy() -> None:
    report = OmniRouteAvailabilityAdapter(
        StaticOmniRouteTransport(
            _catalog("cc/exhausted", "kr/usable"),
            _runtime(
                **{
                    "cc/exhausted": {"quota_remaining_pct": 0.0},
                    "kr/usable": {"quota_remaining_pct": 50.0},
                }
            ),
        )
    ).evaluate(CandidateRequirements(require_usage_evidence=True), now=NOW)

    assert [candidate.model.id for candidate in report.eligible] == ["kr/usable"]
    exhausted = next(candidate for candidate in report.candidates if candidate.model.id == "cc/exhausted")
    assert exhausted.state.value == "quota_exhausted"


def test_runtime_source_selection_uses_every_documented_source_credentials_allow() -> None:
    factory = getattr(omniroute, "configured_runtime_sources", None)
    assert callable(factory)
    assert factory(management_token=None, usage_api_key_id=None) == frozenset({"health"})
    assert factory(management_token="mgmt", usage_api_key_id=None) == frozenset(
        {"health", "rate_limits", "model_cooldowns"}
    )
    assert factory(management_token="mgmt", usage_api_key_id="key-1") == frozenset(
        {"health", "rate_limits", "model_cooldowns", "budget", "token_limits"}
    )


def test_worker_runtime_intersects_canonical_availability_before_ranking() -> None:
    admit = getattr(worker_runtime, "admitted_worker_candidates", None)
    assert callable(admit)

    rows = _catalog("cc/exhausted", "kr/usable", "antigravity/no-usage")["data"]
    assert isinstance(rows, list)
    report = OmniRouteAvailabilityAdapter(
        StaticOmniRouteTransport(
            {"data": rows},
            _runtime(
                **{
                    "cc/exhausted": {"quota_remaining_pct": 0.0},
                    "kr/usable": {"quota_remaining_pct": 80.0},
                    "antigravity/no-usage": {},
                }
            ),
        )
    ).evaluate(CandidateRequirements(require_usage_evidence=True), now=NOW)

    task = WorkerTask(
        required_capabilities=frozenset({"tools"}),
        coding=True,
        allowed_route_prefixes=frozenset({"cc/", "kr/"}),
        excluded_route_ids=frozenset({"cc/active-controller"}),
    )
    prime_selectors = [f"omniroute/{row['id']}" for row in rows]
    candidates = admit(task, rows, prime_selectors, report)

    assert [candidate.route_id for candidate in candidates] == ["kr/usable"]


def test_worker_task_config_fences_active_controller_and_normalizes_sets(monkeypatch) -> None:
    build = getattr(worker_runtime, "worker_task_from_config", None)
    assert callable(build)
    monkeypatch.setenv("VERDICT_CONTROLLER_MODEL", "cc/claude-fable-5-1")
    monkeypatch.setenv("VERDICT_WORKER_ROUTE_PREFIXES", "cc/,kr/")

    task = build({"required_capabilities": ["tools"], "coding": True})

    assert task.required_capabilities == frozenset({"tools"})
    assert task.allowed_route_prefixes == frozenset({"cc/", "kr/"})
    assert "cc/claude-fable-5-1" in task.excluded_route_ids
