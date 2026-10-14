"""OmniRoute passive outcomes are route health, never capability proof."""

from datetime import datetime, timedelta, timezone

import pytest

from verdict.admission import admit
from verdict.admit_prove_confirm import gate_admit_prove_confirm
from verdict.free_tier_admit import FreeTierAdmitReceipt
from verdict.omniroute_health import evidence_from_omniroute_health
from verdict.orchestration.eligibility import EligibilityLadder
from verdict.subagent_selection import HealthResult

NOW = datetime(2026, 10, 14, tzinfo=timezone.utc)
ROUTE = "mistral/codestral-latest"


def payloads():
    return (
        {
            "entries": [
                {
                    "provider": "mistral",
                    "model": "codestral-latest",
                    "key": ROUTE,
                    "totalRequests": 3,
                    "successRate": 0.9,
                    "windowHours": 24,
                }
            ]
        },
        {
            "connections": [
                {
                    "provider": "mistral",
                    "isActive": True,
                    "connectionStatus": "healthy",
                    "cooldownRemainingMs": 0,
                    "breaker": {"state": "CLOSED"},
                    "lockouts": [],
                }
            ]
        },
    )


def evidence():
    return evidence_from_omniroute_health(*payloads(), now=NOW)


def test_live_passive_health_reaches_ladder_without_local_probe(tmp_path):
    rows = [{"id": ROUTE, "owned_by": "mistral"}]
    connections = [{"provider": "mistral", "isActive": True, "testStatus": "active"}]
    admitted = admit(rows, connections, evidence(), now=NOW)
    assert admitted.proven_healthy(ROUTE)
    assert not admitted.proven_healthy("mistral/untested")
    ladder = EligibilityLadder(
        rows,
        connections,
        lambda _: HealthResult(False, "not_probed"),
        tmp_path / "state.json",
        admitted=admitted,
    )
    assert ladder._health_status(ROUTE, NOW) == ("healthy", "")


@pytest.mark.parametrize(
    "change",
    [
        "few",
        "failures",
        "old_window",
        "missing_window",
        "inactive",
        "cooldown",
        "breaker",
        "lockout",
        "unknown",
        "wrong_identity",
    ],
)
def test_passive_health_requires_model_success_and_live_connection(change):
    stats, resilience = payloads()
    row, conn = stats["entries"][0], resilience["connections"][0]
    if change == "few":
        row["totalRequests"] = 2
    elif change == "failures":
        row["successRate"] = 0.89
    elif change == "old_window":
        row["windowHours"] = 169
    elif change == "missing_window":
        row.pop("windowHours")
    elif change == "inactive":
        conn["isActive"] = False
    elif change == "cooldown":
        conn["cooldownRemainingMs"] = 1000
    elif change == "breaker":
        conn["breaker"]["state"] = "OPEN"
    elif change == "lockout":
        conn["lockouts"] = [{"model": "codestral-latest", "remainingMs": 1000}]
    elif change == "unknown":
        conn["connectionStatus"] = "unknown"
    elif change == "wrong_identity":
        row["key"] = "other/codestral-latest"
    assert not evidence_from_omniroute_health(stats, resilience, now=NOW).observations


def test_passive_health_allows_confirm_without_fabricating_passport():
    receipt = FreeTierAdmitReceipt(
        admitted=(ROUTE,),
        exclusions=(),
        chosen=ROUTE,
        empty_intersection=False,
        active_providers=("mistral",),
        free_tier_providers=("mistral",),
        free_admitted=(ROUTE,),
    )
    calls = []

    def transport(model_id, payload, timeout):
        calls.append(model_id)
        return {
            "status_code": 200,
            "body": {
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        }

    result = gate_admit_prove_confirm(
        receipt, passports={}, passive_health=evidence(), confirm_transport=transport, now=NOW
    )
    assert result.chosen == ROUTE
    assert calls == [ROUTE]
    assert result.confirm[0].confirmed
    # Main (#830) records a passport from a successful confirm probe; passive traffic alone never creates one.
    assert result.passport[0].qualified_at == "2026-10-14T00:00:00Z"
    expired = gate_admit_prove_confirm(
        receipt,
        passports={},
        passive_health=evidence(),
        confirm_transport=transport,
        now=NOW + timedelta(minutes=6),
    )
    assert expired.chosen is None
    assert calls == [ROUTE]


def test_confirm_budget_accounts_for_prompt_tokens():
    from verdict.admission import RuntimeEvidence, RuntimeObservation

    second = "mistral/second"
    health = RuntimeEvidence(
        (
            *evidence().observations,
            RuntimeObservation(
                f"route:{second}",
                "healthy",
                "passive_success",
                "fixture",
                NOW.isoformat(),
                (NOW + timedelta(minutes=5)).isoformat(),
            ),
        )
    )
    receipt = FreeTierAdmitReceipt(
        admitted=(ROUTE, second),
        exclusions=(),
        chosen=ROUTE,
        empty_intersection=False,
        active_providers=("mistral",),
        free_tier_providers=("mistral",),
        free_admitted=(ROUTE, second),
    )
    calls = []

    def transport(model_id, payload, timeout):
        calls.append(model_id)
        return {
            "status_code": 200,
            "body": {
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 1, "total_tokens": 21},
            },
        }

    result = gate_admit_prove_confirm(
        receipt, passports={}, passive_health=health, confirm_transport=transport, now=NOW
    )
    assert calls == [ROUTE, second]
    assert result.admitted == (ROUTE, second)


def test_expired_passive_health_cannot_authorize_launch():
    admitted = admit(
        [{"id": ROUTE}],
        [{"provider": "mistral", "isActive": True, "testStatus": "active"}],
        evidence(),
        now=NOW + timedelta(minutes=6),
    )
    assert not admitted.proven_healthy(ROUTE)


def test_http_snapshot_carries_passive_evidence_into_confirm():
    import httpx

    from verdict.free_tier_admit import admit_free_tier_active, load_omniroute_admit_snapshot
    from verdict.omniroute_health import STATS_PATH

    stats, resilience = payloads()
    visited = []

    def handler(request):
        path = request.url.path
        visited.append(path)
        if path == "/v1/models":
            body = {"data": [{"id": ROUTE, "owned_by": "mistral"}]}
        elif path == "/api/providers":
            body = {
                "connections": [{"provider": "mistral", "isActive": True, "testStatus": "active"}]
            }
        elif path == "/api/free-tier/summary":
            body = {
                "perModel": [
                    {"provider": "mistral", "modelId": "codestral-latest", "freeType": "recurring"}
                ]
            }
        elif path == STATS_PATH.split("?")[0]:
            assert request.url.params["windowHours"] == "168"
            body = stats
        elif path == "/api/resilience/connections":
            body = resilience
        else:
            raise AssertionError(path)
        return httpx.Response(200, json=body)

    snapshot = load_omniroute_admit_snapshot(
        "http://127.0.0.1:20128", "test", transport=httpx.MockTransport(handler)
    )
    assert snapshot.passive_health is not None
    assert snapshot.passive_health.observations[0].key == f"route:{ROUTE}"
    assert admit_free_tier_active(snapshot).chosen == ROUTE
    assert len(visited) == 5


def test_build_selector_joins_passive_health(monkeypatch, tmp_path):
    import verdict.omniroute_health as health
    from verdict.orchestration import eligibility_report, run

    monkeypatch.setattr(
        run, "fetch_inventory", lambda *a, **kw: [{"id": ROUTE, "owned_by": "mistral"}]
    )
    monkeypatch.setattr(
        run,
        "fetch_connections",
        lambda *a, **kw: [{"provider": "mistral", "isActive": True, "testStatus": "active"}],
    )
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(
        health,
        "load_omniroute_health",
        lambda *a, **kw: evidence_from_omniroute_health(*payloads(), now=now),
    )
    ladder = eligibility_report.build_selector(
        "http://127.0.0.1:1", scope="", prefer="", state_file=tmp_path / "state.json"
    )
    assert ladder.admitted.proven_healthy(ROUTE)
    assert ladder._health_status(ROUTE, now) == ("healthy", "")


@pytest.mark.parametrize("status", [401, 403, 404, 500])
def test_unavailable_passive_endpoint_fails_closed(status):
    import httpx

    from verdict.omniroute_health import load_omniroute_health

    result = load_omniroute_health(
        "http://127.0.0.1:20128",
        api_key="test",
        now=NOW,
        transport=httpx.MockTransport(lambda _: httpx.Response(status)),
    )
    assert not result.observations
    assert not result.consulted


def test_non_json_passive_body_fails_closed():
    import httpx

    from verdict.omniroute_health import load_omniroute_health

    result = load_omniroute_health(
        "http://127.0.0.1:20128",
        api_key="test",
        now=NOW,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, content=b"<html>not json</html>")
        ),
    )
    assert not result.observations


def test_non_list_entries_fail_closed():
    stats, resilience = payloads()
    stats["entries"] = tuple(stats["entries"])
    assert not evidence_from_omniroute_health(stats, resilience, now=NOW).observations


def test_non_list_connections_fail_closed():
    stats, resilience = payloads()
    resilience["connections"] = tuple(resilience["connections"])
    assert not evidence_from_omniroute_health(stats, resilience, now=NOW).observations
