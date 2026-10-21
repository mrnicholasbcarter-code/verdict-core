"""Native CLI wiring exercises the actual six-GET reader with no writes."""

import json
from datetime import datetime
from urllib.parse import urlsplit

import pytest
from test_identify import NOW, metadata, payloads


def test_native_passive_cli_six_gets_no_inference_or_persistence(monkeypatch, capsys):
    from verdict import cli, identify, omniroute_evidence, prove_at_rest
    from verdict.credentials_store import resolve_gateway_binding
    from verdict.metadata import store
    from verdict.orchestration.health_cache import HealthCache
    from verdict.prove_at_rest_legacy import ProveAtRestStore

    data = payloads()
    calls = []
    paths = {
        "/api/providers": "providers",
        "/api/resilience/connections": "resilience",
        "/api/usage/provider-limits": "limits",
        "/api/resilience/model-cooldowns": "cooldowns",
        "/api/providers/health-matrix": "health_matrix",
        "/v1/models": "v1_models",
    }

    def forbidden(*args, **kwargs):
        pytest.fail("passive identify attempted inference or persistence")

    class Response:
        def __init__(self, payload):
            self._body = json.dumps(payload).encode()
            self._offset = 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, size=-1):
            if size is None or size < 0:
                chunk, self._offset = self._body[self._offset :], len(self._body)
                return chunk
            chunk = self._body[self._offset : self._offset + size]
            self._offset += len(chunk)
            return chunk

    def get_only(request, **kwargs):
        assert request.get_method() == "GET"
        assert request.data is None
        assert request.get_header("Authorization") == "Bearer secret-fixture-key"
        url = urlsplit(request.full_url)
        assert url.netloc == "127.0.0.1:20128"
        calls.append(url.path)
        return Response(data[paths[url.path]])

    monkeypatch.setattr(omniroute_evidence, "open_no_redirect", get_only)
    monkeypatch.setattr(HealthCache, "record", forbidden)
    monkeypatch.setattr(HealthCache, "merge_and_save", forbidden)
    monkeypatch.setattr(store, "save_store", forbidden)
    monkeypatch.setattr(ProveAtRestStore, "write", forbidden)
    monkeypatch.setattr(prove_at_rest, "live_screen_transport", forbidden)
    monkeypatch.setattr(prove_at_rest, "live_transport", forbidden)
    monkeypatch.setattr(identify, "load_store", metadata)
    monkeypatch.setattr(
        identify,
        "resolve_gateway_binding",
        lambda: resolve_gateway_binding(
            environ={
                "OMNIROUTE_BASE_URL": "http://127.0.0.1:20128/v1",
                "OMNIROUTE_API_KEY": "secret-fixture-key",
            }
        ),
    )

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(omniroute_evidence, "datetime", Clock)
    monkeypatch.setattr("sys.argv", ["verdict", "identify", "--passive", "--json"])
    cli.main()
    raw = capsys.readouterr().out
    report = json.loads(raw)
    assert set(calls) == set(paths)
    assert len(calls) == 6
    assert report["model_calls"] == 0
    assert report["available_routes"][0]["route_id"] == "a/best"
    assert "secret-fixture-key" not in raw
    assert "accounts" not in raw


def test_native_cli_requires_explicit_passive_and_refuses_screen(monkeypatch):
    from verdict import cli

    for args in [["identify", "--json"], ["identify", "--screen", "--json"]]:
        monkeypatch.setattr("sys.argv", ["verdict", *args])
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 2


def test_missing_metadata_stays_unknown_and_human_output_is_bounded(monkeypatch, capsys):
    from verdict import identify
    from verdict.omniroute_evidence import parse_omniroute_evidence

    def missing():
        raise FileNotFoundError

    monkeypatch.setattr(identify, "load_store", missing)
    monkeypatch.setattr(
        identify, "resolve_gateway_binding", lambda: ("http://127.0.0.1:20128/v1", None)
    )
    monkeypatch.setattr(
        identify,
        "fetch_omniroute_evidence",
        lambda *a, **kw: parse_omniroute_evidence(**payloads(), now=NOW),
    )
    identify.cmd_identify(output_json=True)
    report = json.loads(capsys.readouterr().out)
    assert report["metadata_error"] == "metadata_unavailable"
    assert all(r["basis"] == "unknown" for r in report["available_routes"])
    identify.cmd_identify(output_json=False)
    assert len(capsys.readouterr().out.splitlines()) <= 5


def test_identify_action_returns_the_cli_domain_report(monkeypatch):
    from verdict import identify
    from verdict.actions.registry import run_action

    report = {"mode": "passive", "model_calls": 0}
    monkeypatch.setattr(identify, "identify_report", lambda: report)
    result = run_action("identify")
    assert result.ok
    assert result.data == report
