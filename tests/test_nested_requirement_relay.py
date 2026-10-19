"""Nested execution_path_request requirements through the real production path.

Client JSON -> ``api._authority_context`` parser -> real ``IntelligenceService.route``
-> FastAPI relay -> stub upstream transport. Nothing here builds the profile digest by
hand: the relay accepts or refuses the send by comparing the digest the route minted
with the digest it recomputes itself, so a requirement that is merged on only one side
shows up as a refused send.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

import verdict.api as api
from tests import test_live_candidate_shortlist as live
from verdict.metadata.records import CapabilityCaps, ModelMetadataRecord, SoftScores
from verdict.metadata.store import MetadataSnapshot
from verdict.proxy import UpstreamProxy

SMALL = live.STRONG  # context window 128k
LARGE = live.NEW  # context window 256k
NEUTRAL_TASK = "note for later"  # no wording that implies any capability
FLAGS = (
    "tools_required",
    "reasoning_required",
    "structured_output_required",
    "streaming_required",
    "vision_required",
)
CAP_FIELD = {
    "tools_required": "tools",
    "reasoning_required": "reasoning",
    "structured_output_required": "structured",
    "streaming_required": "streaming",
    "vision_required": "vision",
}


def _metadata(
    *, small_caps: dict[str, bool | None] | None = None, large_context: int = 256_000
) -> MetadataSnapshot:
    """Trusted metadata: every capability true, except what ``small_caps`` overrides.

    ``None`` leaves the field unknown (absent), which must never count as satisfied.
    """

    def caps(context: int, overrides: dict[str, bool | None]) -> CapabilityCaps:
        values: dict[str, Any] = {
            name: live._prov(overrides.get(name, True))
            if overrides.get(name, True) is not None
            else None
            for name in CAP_FIELD.values()
        }
        return CapabilityCaps(context=live._prov(context), **values)

    rows = ((SMALL, caps(128_000, small_caps or {}), 95.0), (LARGE, caps(large_context, {}), 90.0))
    return MetadataSnapshot(
        schema_version="1",
        refreshed_at=live.FETCHED,
        sources={},
        records=tuple(
            ModelMetadataRecord(
                id=model_id,
                provider=model_id.split("/", 1)[0],
                omniroute_ids=(model_id,),
                caps=model_caps,
                scores=SoftScores(aa_coding=live._prov(score)),
            )
            for model_id, model_caps, score in rows
        ),
    )


def _candidate(route_id: str) -> dict[str, Any]:
    provider, model = route_id.split("/", 1)
    return {
        "route_id": route_id,
        "gateway": "omniroute",
        "provider": provider,
        "model": model,
        "eligible": True,
        "is_free": True,
        "certification_state": "ready",
        "certification_freshness": "fresh",
    }


def _send(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    offered: tuple[str, ...],
    nested: dict[str, Any] | None = None,
    top_level: dict[str, Any] | None = None,
    metadata: MetadataSnapshot | None = None,
) -> tuple[httpx.Response, list[str], dict[str, Any]]:
    """POST one chat request; return (response, upstream models sent, observations)."""
    current = datetime.now(timezone.utc)
    monkeypatch.setattr(live, "NOW", current)
    monkeypatch.setenv("VERDICT_HEALTH_CACHE", str(tmp_path / "health.json"))
    monkeypatch.setenv("LLMGATE_ALLOW_ANONYMOUS", "true")
    service = live._service([], top_k=4)
    service.admit_now = current
    service.metadata_snapshot = metadata or _metadata()
    service.passports = {rid: live._passport(rid, latency_ms=1.0) for rid in (SMALL, LARGE)}
    sent: list[str] = []
    seen: dict[str, Any] = {"route_digests": [], "relay_profiles": []}

    async def upstream(request: httpx.Request) -> httpx.Response:
        model = json.loads(await request.aread())["model"]
        sent.append(model)
        return httpx.Response(200, json={"model": model, "choices": []})

    class Intelligence:
        primary_model = service.primary_model
        providers = service.providers
        log_path = ""
        log_full_task = False
        discovery_ttl = 60
        profile = "production"
        planner = service.planner

        async def route(self, task: str, **kwargs: Any) -> Any:
            decision = await service.route(task, **kwargs)
            seen["route_digests"] = sorted(
                {p.scope.profile_digest for p in decision.admission_proofs.values()}
            )
            return decision

        def readiness(self) -> Any:
            return service.readiness()

    real_profile = api.prepared_task_profile

    def recording_profile(*args: Any, **kwargs: Any) -> Any:
        result = real_profile(*args, **kwargs)
        seen["relay_profiles"].append(result[1])
        return result

    monkeypatch.setattr(api, "prepared_task_profile", recording_profile)
    monkeypatch.setattr(api, "_build_intelligence", Intelligence)
    monkeypatch.setattr(
        api,
        "_build_proxy",
        lambda: UpstreamProxy("http://upstream.test/v1", transport=httpx.MockTransport(upstream)),
    )
    body = {
        "messages": [{"role": "user", "content": NEUTRAL_TASK}],
        "task_family": "coding",
        "spend_policy": "free_only",
        "execution_path_request": {
            "schema_version": "1",
            "trajectory_id": "traj-nested",
            "slice_id": "slice-nested",
            "acceptance_criteria": ["done"],
            "proof_criteria": ["check"],
            "candidates": [_candidate(route_id) for route_id in offered],
            **(nested or {}),
        },
        **(top_level or {}),
    }
    with TestClient(api.app) as client:
        response = client.post("/v1/chat/completions", json=body)
    return response, sent, seen


def _model(route_id: str) -> str:
    return route_id.split("/", 1)[1]


def test_neutral_task_with_no_requirement_may_use_the_weakest_route(monkeypatch, tmp_path) -> None:
    """Control: wording alone must not create a requirement, or the cases below prove nothing."""
    weak = _metadata(small_caps={name: False for name in CAP_FIELD.values()})
    response, sent, _ = _send(monkeypatch, tmp_path, offered=(SMALL,), metadata=weak)
    assert response.status_code == 200, response.text
    assert sent == [_model(SMALL)]


@pytest.mark.parametrize("where", ["nested", "top_level"])
def test_context_requirement_satisfied_by_capable_route_sends_once_with_same_profile(
    where: str, monkeypatch, tmp_path
) -> None:
    requirement = {"min_context_tokens": 200_000}
    response, sent, seen = _send(
        monkeypatch,
        tmp_path,
        offered=(SMALL, LARGE),
        nested=requirement if where == "nested" else None,
        top_level=requirement if where == "top_level" else None,
    )
    assert response.status_code == 200, response.text
    assert sent == [_model(LARGE)]
    relay_profiles = seen["relay_profiles"]
    assert relay_profiles, "relay never recomputed a profile"
    assert {p.min_context for p in relay_profiles} == {200_000}
    # The proofs minted by route() are bound to the digest the relay recomputed.
    assert seen["route_digests"] == sorted({p.digest for p in relay_profiles})


def test_nested_context_above_every_window_sends_nothing(monkeypatch, tmp_path) -> None:
    response, sent, _ = _send(
        monkeypatch, tmp_path, offered=(SMALL,), nested={"min_context_tokens": 200_000}
    )
    assert sent == []
    assert response.status_code != 200
    assert "capability_mismatch" in response.text, response.text  # named refusal


@pytest.mark.parametrize(
    ("nested_value", "top_value"),
    [(1_000, 200_000), (200_000, 1_000), (200_000, None), (None, 200_000)],
    ids=["nested-lower", "top-lower", "nested-only", "top-only"],
)
def test_context_floor_is_the_strongest_value_from_either_side(
    nested_value: int | None, top_value: int | None, monkeypatch, tmp_path
) -> None:
    response, sent, _ = _send(
        monkeypatch,
        tmp_path,
        offered=(SMALL,),
        nested=None if nested_value is None else {"min_context_tokens": nested_value},
        top_level=None if top_value is None else {"min_context_tokens": top_value},
    )
    assert sent == []
    assert response.status_code != 200, "a 128k route served a 200k floor"


@pytest.mark.parametrize("flag", FLAGS)
@pytest.mark.parametrize("where", ["nested", "top_level", "both"])
def test_required_capability_excludes_route_that_lacks_it(
    flag: str, where: str, monkeypatch, tmp_path
) -> None:
    """Neutral text: only trusted metadata can make this requirement bite."""
    lacking = _metadata(small_caps={CAP_FIELD[flag]: False})
    response, sent, seen = _send(
        monkeypatch,
        tmp_path,
        offered=(SMALL, LARGE),
        nested={flag: True} if where in {"nested", "both"} else None,
        top_level={flag: True} if where in {"top_level", "both"} else None,
        metadata=lacking,
    )
    assert response.status_code == 200, response.text
    assert sent == [_model(LARGE)], f"{flag} ({where}) did not exclude the route lacking it"
    assert {p.required_capabilities for p in seen["relay_profiles"]} == {(CAP_FIELD[flag],)}
    assert seen["route_digests"] == sorted({p.digest for p in seen["relay_profiles"]})


@pytest.mark.parametrize("flag", FLAGS)
@pytest.mark.parametrize("unknown", [False, True], ids=["declared-false", "unknown"])
def test_nested_capability_with_no_capable_route_sends_nothing(
    flag: str, unknown: bool, monkeypatch, tmp_path
) -> None:
    """A missing capability is insufficient, never optimistically satisfied."""
    lacking = _metadata(small_caps={CAP_FIELD[flag]: None if unknown else False})
    response, sent, _ = _send(
        monkeypatch, tmp_path, offered=(SMALL,), nested={flag: True}, metadata=lacking
    )
    assert sent == []
    assert response.status_code != 200
    assert "strategy authority blocked" in response.text, response.text


@pytest.mark.parametrize("flag", FLAGS)
def test_nested_false_cannot_undo_top_level_true(flag: str, monkeypatch, tmp_path) -> None:
    lacking = _metadata(small_caps={CAP_FIELD[flag]: False})
    response, sent, _ = _send(
        monkeypatch,
        tmp_path,
        offered=(SMALL,),
        nested={flag: False},
        top_level={flag: True},
        metadata=lacking,
    )
    assert sent == []
    assert response.status_code != 200
