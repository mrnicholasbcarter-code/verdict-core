"""Behavioral regressions for scripts/quickstart.py route-probe truthfulness.

Parent fail-closed checks use fake child results; integration cases run the
probe against real in-process API/auth/admission/optimizer paths with fixture
evidence and blocked network. No install or live gateway is used.
A probe passes only when the child exits 0,
the first stdout line is HTTP 200, and the remaining stdout is one JSON object
whose ``decision`` field is a non-empty string other than ``denied``.
"""

from __future__ import annotations

import importlib.util
import io
import json
import shlex
import subprocess
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

# Keep only the in-process ASGI client usable under no_gateway_network. All
# other HTTP clients and the socket layer remain blocked by that fixture.
_ASGI_SEND = httpx.Client.send

ROOT = Path(__file__).parents[1]
QUICKSTART = ROOT / "scripts" / "quickstart.py"


def _load():
    spec = importlib.util.spec_from_file_location("quickstart_under_test", QUICKSTART)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _completed(stdout: str, *, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["python", "-c", "probe"], returncode=returncode, stdout=stdout, stderr=""
    )


def _route_output(status: int, body: object) -> str:
    return f"{status}\n{json.dumps(body)}\n"


@pytest.fixture
def quickstart(monkeypatch):
    module = _load()

    def fake_run(cmd, cwd=None, check=True, env=None):
        text = cmd if isinstance(cmd, str) else " ".join(cmd)
        if " -c " in text or text.rstrip().endswith('-c "'):
            return module._probe_result
        return _completed("", returncode=0)

    monkeypatch.setattr(module, "run", fake_run)
    monkeypatch.setattr(module.tempfile, "TemporaryDirectory", lambda: _NullTemp())
    return module


class _NullTemp:
    def __enter__(self):
        return "/tmp/quickstart-probe-unused"

    def __exit__(self, *args):
        return False


def _run_main(module, stdout: str, *, returncode: int = 0) -> int | None:
    """Return the SystemExit code, or None when main returns without exiting."""
    module._probe_result = _completed(stdout, returncode=returncode)
    try:
        module.main()
    except SystemExit as caught:
        assert isinstance(caught.code, int)
        return caught.code
    return None


def test_valid_200_non_denied_decision_continues(quickstart, capsys) -> None:
    body = {"decision": "selected", "model": "example", "reason": "fit"}
    code = _run_main(quickstart, _route_output(200, body))
    assert code is None
    assert "RESULT: PASS" in capsys.readouterr().out


@pytest.mark.parametrize(
    "stdout",
    [
        _route_output(503, {"error": {"message": "server authentication is not configured"}}),
        _route_output(422, {"detail": [{"loc": ["body", "task"], "msg": "Field required"}]}),
        "200\nnot-json\n",
        _route_output(200, ["not", "an", "object"]),
        _route_output(200, {"model": "example"}),
        _route_output(200, {"decision": ""}),
        _route_output(200, {"decision": "denied", "reason": "managed intelligence unavailable"}),
        "503\n",
    ],
)
def test_unsuccessful_probe_exits_nonzero_and_does_not_pass(
    quickstart, capsys, stdout: str
) -> None:
    code = _run_main(quickstart, stdout)
    captured = capsys.readouterr().out
    assert code not in (None, 0)
    assert "RESULT: PASS" not in captured
    assert "QUICKSTART PASSED" not in captured


def test_child_probe_failure_is_nonzero(quickstart, capsys) -> None:
    code = _run_main(quickstart, "", returncode=1)
    captured = capsys.readouterr().out
    assert code not in (None, 0)
    assert "RESULT: PASS" not in captured


@pytest.fixture
def route_api(monkeypatch, tmp_path, no_gateway_network):
    """Real API/auth/admission/optimizer; only evidence inputs are fixtures.

    No route method, auth dependency, admission gate or optimizer is replaced.
    The service's explicit snapshot avoids admission discovery. Lifespan's
    independent inventory refresh still resolves the default local endpoint;
    the network guard blocks those attempts. Zero confirms does not mean zero
    inventory attempts.
    """
    import verdict.api as api
    from tests import test_live_candidate_shortlist as live

    monkeypatch.setenv("VERDICT_HOME", str(tmp_path / "verdict"))
    monkeypatch.setenv("VERDICT_HEALTH_CACHE", str(tmp_path / "health.json"))
    monkeypatch.setenv("LLMGATE_AUTH_TOKEN", "quickstart-test-token")
    monkeypatch.setattr(TestClient, "send", _ASGI_SEND)
    monkeypatch.setattr(live, "NOW", datetime.now(timezone.utc))
    probed = []
    service = live._service(probed)
    service.providers = {}
    monkeypatch.setattr(api, "_build_intelligence", lambda: service)
    candidate = {
        "route_id": live.STRONG,
        "gateway": "omniroute",
        "provider": live.STRONG.split("/", 1)[0],
        "model": live.STRONG.split("/", 1)[1],
        "eligible": True,
        "is_free": True,
        "certification_state": "ready",
        "certification_freshness": "fresh",
    }
    payload = {
        "task": "note for later",
        "task_family": "coding",
        "execution_path_request": {
            "schema_version": "1",
            "trajectory_id": "quickstart-fixture",
            "slice_id": "route-only",
            "acceptance_criteria": ["route selected"],
            "proof_criteria": ["server admission and optimizer accept the route"],
            "candidates": [candidate],
        },
    }
    return api, payload, probed, service


def test_probe_uses_real_schema_and_execution_authority(route_api, monkeypatch, tmp_path, capsys):
    api, payload, probed, _ = route_api
    path = tmp_path / "route.json"
    path.write_text(json.dumps(payload))
    monkeypatch.setenv("VERDICT_QUICKSTART_ROUTE_REQUEST", str(path))
    module = _load()
    module._run_route_probe()
    result = _completed(capsys.readouterr().out)
    assert module._route_probe_succeeded(result)
    body = json.loads("\n".join(result.stdout.splitlines()[1:]))
    assert body["decision"] == "selected"
    from verdict.serve_path import SERVE_PATH_AUTHORITY_FLAG

    assert SERVE_PATH_AUTHORITY_FLAG in body["safety_flags"]
    candidate = payload["execution_path_request"]["candidates"][0]
    assert body["model"] == candidate["model"]
    assert body["provider"] == candidate["provider"]
    assert f"bod104_route_id:{candidate['route_id']}" in body["safety_flags"]
    assert probed == []  # fresh fixture passports, no confirm or provider calls
    assert api.intelligence_instance is None  # lifespan actually closed


def test_main_runs_actual_probe_command_in_process(route_api, monkeypatch, tmp_path, capsys):
    """Exercise the generated command, not a substring assertion about its text."""
    from contextlib import nullcontext

    _, payload, probed, _ = route_api
    path = tmp_path / "request.json"
    path.write_text(json.dumps(payload))
    monkeypatch.setenv("VERDICT_QUICKSTART_ROUTE_REQUEST", str(path))
    module = _load()
    commands = []

    def run_in_process(cmd, cwd=None, check=True, env=None):
        commands.append(cmd)
        if " -c " not in cmd:
            return _completed("")  # never create venvs, install or spawn CLI/tests
        assert env["HOME"] == str(tmp_path)
        assert env["VERDICT_HOME"] == str(tmp_path / "verdict")
        assert env["VERDICT_RECEIPTS_DB"] == str(tmp_path / "probe-receipts.db")
        assert "quickstart-test-token" not in cmd
        output = io.StringIO()
        with monkeypatch.context() as child_env, redirect_stdout(output):
            for key, value in env.items():
                child_env.setenv(key, value)
            exec(shlex.split(cmd)[2], {})
        return _completed(output.getvalue())

    monkeypatch.setattr(module, "run", run_in_process)
    monkeypatch.setattr(module.tempfile, "TemporaryDirectory", lambda: nullcontext(str(tmp_path)))
    module.main()
    assert "RESULT: PASS" in capsys.readouterr().out
    assert any("-m verdict --help" in cmd for cmd in commands)
    assert any("-m pytest tests/test_contracts.py tests/test_models.py" in cmd for cmd in commands)
    assert probed == []


@pytest.mark.parametrize(
    ("authorization", "expected"),
    [(None, 401), ("Bearer wrong", 403), ("Bearer quickstart-test-token", 200)],
)
def test_real_api_bearer_behavior(route_api, authorization, expected):
    api, payload, probed, _ = route_api
    headers = {"Authorization": authorization} if authorization else {}
    with TestClient(api.app) as client:
        response = client.post("/v1/route", json=payload, headers=headers)
    assert response.status_code == expected
    if expected == 401:
        assert response.headers["www-authenticate"] == "Bearer"
    assert probed == []


def test_real_api_missing_task_is_422(route_api):
    api, payload, _, _ = route_api
    del payload["task"]
    with TestClient(api.app) as client:
        response = client.post(
            "/v1/route", json=payload, headers={"Authorization": "Bearer quickstart-test-token"}
        )
    assert response.status_code == 422
    assert any(item["loc"] == ["body", "task"] for item in response.json()["detail"])


def test_real_api_absent_auth_config_is_503(route_api, monkeypatch):
    api, payload, _, _ = route_api
    # Startup itself requires configured auth; exercising middleware without
    # lifespan is intentional here, not a way to make the positive probe pass.
    monkeypatch.delenv("LLMGATE_AUTH_TOKEN")
    response = TestClient(api.app).post("/v1/route", json=payload)
    assert response.status_code == 503
    assert response.json()["error"]["message"] == "server authentication is not configured"
    with pytest.raises(ValueError, match="production server requires"), TestClient(api.app):
        pass


@pytest.mark.parametrize(
    "mutation", ["missing", "client_decision", "ineligible", "not_admitted", "unproven"]
)
def test_real_api_does_not_invent_authority(route_api, mutation):
    api, payload, probed, service = route_api
    if mutation == "missing":
        del payload["execution_path_request"]
    elif mutation == "client_decision":
        payload["execution_path_decision"] = {"decision": "selected"}
    elif mutation == "unproven":
        service.passports = {}
        service.confirm_transport = None
    else:
        candidate = payload["execution_path_request"]["candidates"][0]
        if mutation == "ineligible":
            candidate["eligible"] = False
        else:
            candidate.update(route_id="unadmitted/model", provider="unadmitted", model="model")
    with TestClient(api.app) as client:
        response = client.post(
            "/v1/route", json=payload, headers={"Authorization": "Bearer quickstart-test-token"}
        )
    assert response.status_code == 400
    assert not _load()._route_probe_succeeded(_completed(_route_output(400, response.json())))
    assert probed == []


@pytest.mark.parametrize("raw", [None, "not-json", "[]", '{"task": "only task"}'])
def test_probe_requires_explicit_request_without_leaking_contents(
    monkeypatch, tmp_path, capsys, raw
):
    module = _load()
    if raw is None:
        monkeypatch.delenv("VERDICT_QUICKSTART_ROUTE_REQUEST", raising=False)
    else:
        path = tmp_path / "route.json"
        path.write_text(raw)
        monkeypatch.setenv("VERDICT_QUICKSTART_ROUTE_REQUEST", str(path))
    with pytest.raises(SystemExit) as caught:
        module._run_route_probe()
    assert caught.value.code == 1
    output = capsys.readouterr().out
    assert "RESULT: PASS" not in output
    if raw:
        assert raw not in output
