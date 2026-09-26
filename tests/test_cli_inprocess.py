"""In-process tests for CLI command helpers."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, ClassVar

import pytest
import yaml

from verdict import cli
from verdict.execution_packet import ExecutionPacket
from verdict.models import RoutingDecision
from verdict.provider_detection import DetectedProvider, DetectionResult


@pytest.fixture(autouse=True)
def isolated_config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))


def test_cmd_route_terse_uses_configured_primary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg_dir = tmp_path / ".config" / "verdict"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "verdict.yaml").write_text(
        "primary_model: test-primary\n"
        "log_path: route-log.jsonl\n"
        "providers:\n"
        "  cheap:\n"
        "    base_url: http://localhost:1234/v1\n"
    )

    monkeypatch.setattr(
        cli, "execute_offload_chat", lambda *_args, **_kwargs: ("sent", "completed")
    )
    cli.cmd_route("deploy prod", "critical", terse=True, allow_legacy_selector=True)

    assert capsys.readouterr().out.strip() == "test-primary"


def test_cmd_route_and_run_send_configured_provider_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """default route/run must actually send, not preview with not_sent."""
    cfg_dir = tmp_path / ".config" / "verdict"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "verdict.yaml").write_text(
        "primary_model: cheap/model\n"
        "log_path: route-log.jsonl\n"
        "providers:\n"
        "  omniroute:\n"
        "    base_url: http://127.0.0.1:20128/v1\n"
        "    api_key_env: OMNIROUTE_API_KEY\n"
    )
    monkeypatch.setenv("OMNIROUTE_API_KEY", "test-token")

    sent: list[tuple[str, str, str]] = []

    def fake_send(base_url: str, model_id: str, task: str, **kwargs: object) -> tuple[str, str]:
        sent.append((base_url, model_id, task))
        return "sent", "hello from omniroute"

    monkeypatch.setattr("verdict.free_tier_admit.execute_offload_chat", fake_send)
    monkeypatch.setattr("verdict.cli.execute_offload_chat", fake_send, raising=False)

    selected = RoutingDecision(
        model="openrouter/free-model",
        provider="omniroute",
        tier=3,
        reason="test selection",
        decision="selected",
        transport_outcome="not_sent",
        request_id="req-bod136",
    )

    class _LiveGate:
        providers: ClassVar[dict[str, cli.ProviderConfig]] = {
            "omniroute": cli.ProviderConfig(
                base_url="http://127.0.0.1:20128/v1", api_key_env="OMNIROUTE_API_KEY"
            )
        }

        def route(self, task: str, criticality: str, context: object = None) -> RoutingDecision:
            del task, criticality, context
            return selected

        def route_with_strategy(self, task: str, criticality: str, context: object = None):
            from verdict.gate import strategy_from_decision

            decision = self.route(task, criticality, context)
            return decision, strategy_from_decision(decision)

    monkeypatch.setattr(cli, "_build_route_gate", lambda allow_offline=False: _LiveGate())

    cli.cmd_route("format a bullet list", "low", terse=False)
    out = capsys.readouterr().out
    assert sent == [("http://127.0.0.1:20128/v1", "openrouter/free-model", "format a bullet list")]
    assert '"transport_outcome": "sent"' in out
    assert "hello from omniroute" in out
    assert "req-bod136" in out

    sent.clear()
    cli.cmd_run("format a bullet list", "low", terse=True)
    assert sent == [("http://127.0.0.1:20128/v1", "openrouter/free-model", "format a bullet list")]
    assert capsys.readouterr().out.strip() == "openrouter/free-model"


def test_cmd_route_identity_mismatch_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg_dir = tmp_path / ".config" / "verdict"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "verdict.yaml").write_text(
        "primary_model: cheap/model\nproviders:\n  omniroute:\n    base_url: http://127.0.0.1:20128/v1\n"
    )

    def fake_send(base_url: str, model_id: str, task: str, **kwargs: object) -> tuple[str, str]:
        del base_url, model_id, task, kwargs
        return "error", "identity mismatch: selected 'cheap/model', served 'other/model'"

    monkeypatch.setattr("verdict.free_tier_admit.execute_offload_chat", fake_send)
    monkeypatch.setattr("verdict.cli.execute_offload_chat", fake_send, raising=False)

    selected = RoutingDecision(
        model="cheap/model",
        provider="omniroute",
        tier=3,
        reason="test selection",
        decision="selected",
        transport_outcome="not_sent",
    )

    class _LiveGate:
        providers: ClassVar[dict[str, cli.ProviderConfig]] = {
            "omniroute": cli.ProviderConfig(base_url="http://127.0.0.1:20128/v1")
        }

        def route(self, task: str, criticality: str, context: object = None) -> RoutingDecision:
            del task, criticality, context
            return selected

        def route_with_strategy(self, task: str, criticality: str, context: object = None):
            from verdict.gate import strategy_from_decision

            decision = self.route(task, criticality, context)
            return decision, strategy_from_decision(decision)

    monkeypatch.setattr(cli, "_build_route_gate", lambda allow_offline=False: _LiveGate())
    with pytest.raises(SystemExit) as exc:
        cli.cmd_route("format docs", "low")
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "identity mismatch" in out
    assert '"transport_outcome": "error"' in out


def test_cmd_route_offline_is_named_fail_closed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    selected = RoutingDecision(
        model="cheap/model",
        provider="omniroute",
        tier=3,
        reason="selected but not executed",
        decision="selected",
        transport_outcome="not_sent",
    )

    class _Gate:
        providers: ClassVar[dict[str, cli.ProviderConfig]] = {}

        def route(self, task: str, criticality: str, context: object = None) -> RoutingDecision:
            del task, criticality, context
            return selected

    monkeypatch.setattr(cli, "_build_route_gate", lambda allow_offline=False: _Gate())
    with pytest.raises(SystemExit) as exc:
        cli.cmd_route("format docs", "low", terse=True, allow_offline=True)
    assert exc.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["transport_outcome"] == "error"
    assert payload["reason"] == "offline routing does not execute a provider completion"


def test_cmd_route_allow_offline_does_not_enable_legacy_selector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """offline catalog mode must not silently set CONTEXT_ALLOW_LEGACY."""
    from verdict.serve_path import CONTEXT_ALLOW_LEGACY

    cfg_dir = tmp_path / ".config" / "verdict"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "verdict.yaml").write_text(
        "primary_model: test-primary\n"
        "log_path: route-log.jsonl\n"
        "providers:\n"
        "  cheap:\n"
        "    base_url: http://localhost:1234/v1\n"
    )

    captured: dict[str, object] = {}

    class _FakeGate:
        def route(self, _task: str, _criticality: str, context: object = None) -> RoutingDecision:
            captured["context"] = context
            return RoutingDecision(
                model="test-primary",
                provider="offline",
                tier=3,
                reason="offline selection",
                decision="selected",
            )

    monkeypatch.setattr(cli, "_build_route_gate", lambda allow_offline=False: _FakeGate())
    with pytest.raises(SystemExit) as exc:
        cli.cmd_route("ping", "low", terse=True, allow_offline=True)
    assert exc.value.code == 1

    ctx = captured.get("context")
    assert ctx is None or CONTEXT_ALLOW_LEGACY not in ctx


def test_setup_dry_run_json_is_mutation_free_and_does_not_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    def fail_if_probed(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("setup dry-run must not probe providers")

    monkeypatch.setattr("verdict.provider_detection.detect_all_providers", fail_if_probed)
    cli.cmd_setup(dry_run=True, output_json=True, non_interactive=True)

    report = json.loads(capsys.readouterr().out)
    assert report["kind"] == "setup_plan"
    assert report["schema_version"] == "1"
    assert report["mutation_free"] is True
    assert report["network_access"] == "disabled"
    assert report["credential_access"] == "disabled"
    assert report["config"]["exists"] is False
    assert report["actions"][0]["action_id"] == "create-config"
    assert report["actions"][0]["requires_consent"] is True
    assert not (tmp_path / "config").exists()


def test_setup_plan_preserves_existing_config_without_reading_contents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config_dir = tmp_path / "config" / "verdict"
    config_dir.mkdir(parents=True)
    config_path = config_dir / "verdict.yaml"
    config_path.write_text("primary_model: keep-me\n", encoding="utf-8")
    before = config_path.read_bytes()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    cli.cmd_setup(dry_run=True, output_json=True)

    report = json.loads(capsys.readouterr().out)
    assert report["config"]["exists"] is True
    assert report["actions"][0]["action_id"] == "preserve-config"
    assert config_path.read_bytes() == before


def test_setup_plan_digest_is_deterministic_and_excludes_digest_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    from verdict.setup_plan import build_setup_plan

    plan = build_setup_plan()
    payload = plan.to_dict()

    assert plan.digest.startswith("sha256:")
    assert plan.digest == plan.plan_digest == plan.plan_id
    assert payload["plan_digest"] == plan.digest
    assert payload["plan_id"] == plan.digest

    payload["plan_digest"] = "sha256:" + "0" * 64
    payload["plan_id"] = "tampered"
    assert plan.digest == build_setup_plan().digest


def test_setup_plan_cli_alias_is_read_only_and_json_compatible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(
        "verdict.provider_detection.detect_all_providers",
        lambda: pytest.fail("setup plan must not discover providers"),
    )
    monkeypatch.setattr("sys.argv", ["verdict", "setup", "plan", "--json"])

    cli.main()

    report = json.loads(capsys.readouterr().out)
    assert report["kind"] == "setup_plan"
    assert report["plan_id"] == report["plan_digest"]
    assert report["mutation_free"] is True
    assert not (tmp_path / "config").exists()


def test_omniroute_token_ignores_private_sqlite_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3

    private_dir = tmp_path / ".omniroute"
    private_dir.mkdir()
    database = private_dir / "storage.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE api_keys (key TEXT, is_active INTEGER, name TEXT, id INTEGER)"
        )
        connection.execute("INSERT INTO api_keys VALUES ('private-db-token', 1, 'default', 1)")
        connection.commit()

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("OMNIROUTE_API_KEY", raising=False)

    assert cli._read_omniroute_token() is None


def test_omniroute_token_uses_explicit_environment_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIROUTE_API_KEY", "explicit-env-token")

    assert cli._read_omniroute_token() == "explicit-env-token"


def test_omniroute_token_does_not_inspect_home_or_private_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_if_path_checked(*_args: object, **_kwargs: object) -> bool:
        raise AssertionError("credential lookup must not inspect private paths")

    monkeypatch.setattr(cli.os.path, "exists", fail_if_path_checked)
    monkeypatch.setattr(cli.os.path, "expanduser", fail_if_path_checked)
    monkeypatch.delenv("OMNIROUTE_API_KEY", raising=False)

    assert cli._read_omniroute_token() is None


def test_omniroute_management_requests_return_none_when_no_local_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T007: with nothing on :20128/:20129, the request still fails gracefully."""
    import urllib.request
    from urllib.error import URLError

    monkeypatch.delenv("OMNIROUTE_BASE_URL", raising=False)
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(URLError("connection refused")),
    )

    assert cli._omniroute_api_request("GET", "/api/provider-nodes") is None


def test_omniroute_management_requests_fall_back_to_local_gateway_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T007: OMNIROUTE_BASE_URL unset but a local gateway answers /api/health."""
    import urllib.request

    seen: list[str] = []

    class Response:
        def __init__(self, body: bytes) -> None:
            self._body = body

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return self._body

    def fake_urlopen(request: urllib.request.Request, *, timeout: float) -> Response:
        url = str(request.full_url)
        seen.append(url)
        if url == "http://localhost:20128/api/health":
            return Response(b'{"status": "ok"}')
        if url == "http://localhost:20128/api/provider-nodes":
            return Response(b'{"ok": true}')
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.delenv("OMNIROUTE_BASE_URL", raising=False)
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    assert cli._omniroute_api_request("GET", "/api/provider-nodes") == {"ok": True}
    assert "http://localhost:20128/api/health" in seen
    assert "http://localhost:20128/api/provider-nodes" in seen


def test_omniroute_management_requests_use_configured_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import urllib.request

    seen: list[str] = []

    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"ok": true}'

    def fake_urlopen(request: urllib.request.Request, *, timeout: float) -> Response:
        seen.append(str(request.full_url))
        assert timeout == 5
        return Response()

    monkeypatch.setenv("OMNIROUTE_BASE_URL", "http://127.0.0.1:24000/management")
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    assert cli._omniroute_api_request("GET", "/api/provider-nodes") == {"ok": True}
    assert seen == ["http://127.0.0.1:24000/management/api/provider-nodes"]


def _packet_file(tmp_path: Path) -> Path:
    payload = {
        "schema_version": "1",
        "packet_id": "packet-cli",
        "packet_version": 1,
        "story_id": "US1",
        "story_version": "1",
        "source": {
            "repository": "git@example/repo",
            "worktree": str(tmp_path),
            "commit": "a" * 40,
            "branch": "feature/test",
            "dirty_digest": "sha256:" + "a" * 64,
            "lock_digests": {},
        },
        "intent": {
            "goal": "Do one bounded task.",
            "non_goals": [],
            "acceptance": ["Focused verification passes."],
            "limitations": [],
        },
        "authority": {
            "owned_paths": ["a.py"],
            "denied_paths": [],
            "tools": ["read"],
            "network": False,
            "max_spend_usd": 0,
            "max_concurrency": 1,
            "max_attempts": 1,
            "destructive": False,
            "production": False,
        },
        "verification": {"argv": ["pytest", "a.py"], "timeout_seconds": 30},
        "decisions": [],
        "context_refs": [],
        "tasks": [
            {"task_id": "T1", "description": "Work.", "status": "pending", "dependencies": []}
        ],
        "route_attempts": [],
        "failure_history": [],
        "transitions": [],
        "checkpoint_refs": [],
        "receipt_refs": [],
        "next_safe_action": "Run the test.",
        "proof_level": "source-only",
    }
    path = tmp_path / "packet.json"
    path.write_text(json.dumps(ExecutionPacket.from_dict(payload).to_dict()), encoding="utf-8")
    return path


def test_autodev_packet_inspect_validate_and_resume_are_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _packet_file(tmp_path)
    before = path.read_bytes()

    for action, extra in (
        ("inspect", []),
        ("validate", []),
        ("resume", ["--model", "cc/claude-sonnet-5"]),
    ):
        monkeypatch.setattr(
            "sys.argv",
            ["verdict", "autodev", "packet", action, "--packet", str(path), "--json", *extra],
        )
        cli.main()
        payload = json.loads(capsys.readouterr().out)
        assert payload["packet_id"] == "packet-cli"
        assert payload["integrity_digest"].startswith("sha256:")

    assert path.read_bytes() == before


def test_autodev_packet_create_refuses_existing_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _packet_file(tmp_path)
    monkeypatch.setattr(
        "sys.argv",
        ["verdict", "autodev", "packet", "create", "--packet", str(path), "--from", str(path)],
    )

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 1


def test_cmd_route_verbose_without_config(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.cmd_route("format docs", "low", terse=False, allow_legacy_selector=True)
    assert exc.value.code == 1

    out = capsys.readouterr().out
    assert "Routing Decision" in out
    assert "format docs" in out
    assert '"transport_outcome": "error"' in out


def test_cmd_stats_handles_missing_and_populated_logs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "missing.jsonl"
    cli.cmd_stats(str(missing))
    assert "No log file found" in capsys.readouterr().out

    log_path = tmp_path / "decisions.jsonl"
    log_path.write_text(
        json.dumps({"decision": {"tier": 0, "model": "frontier", "latency_ms": 10.0}})
        + "\n"
        + "not-json\n"
        + json.dumps({"decision": {"tier": 3, "model": "cheap", "latency_ms": 20.0}})
        + "\n"
    )

    cli.cmd_stats(str(log_path))

    out = capsys.readouterr().out
    assert "Tier Distribution" in out
    assert "Total Requests" in out
    assert "frontier" in out
    assert "cheap" in out


def test_cmd_cost_report_handles_missing_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    cli.cmd_cost_report()

    out = capsys.readouterr().out
    assert "Cost and Usage Report" in out
    assert "No routing telemetry found" in out


def test_cmd_detect_json_and_config(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    result = DetectionResult(
        centralized_routers=[
            DetectedProvider(
                id="9router",
                name="9router",
                type="centralized_router",
                base_url="http://localhost:20128/v1",
                models=["router-primary"],
                server_running=True,
            )
        ]
    )
    monkeypatch.setattr(cli, "detect_all_providers", lambda: result, raising=False)

    # Patch imported provider-detection functions through module import path used by cmd_detect.
    import verdict.provider_detection as provider_detection

    monkeypatch.setattr(provider_detection, "detect_all_providers", lambda: result)

    cli.cmd_detect(output_json=True)
    assert '"centralized_routers"' in capsys.readouterr().out

    cli.cmd_detect(output_config=True)
    out = capsys.readouterr().out
    assert "primary_model: router-primary" in out
    assert "9router:" in out

    cli.cmd_detect(verbose=True)
    assert "Centralized router detected" in capsys.readouterr().out


def test_cmd_detect_reports_multiple_healthy_gateways(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """T018/T019: multiple healthy gateways are all listed with a selection hint."""
    import verdict.provider_detection as provider_detection
    from verdict.provider_detection import GatewayCandidate

    monkeypatch.setattr(provider_detection, "detect_all_providers", lambda: DetectionResult())
    monkeypatch.setattr(
        provider_detection,
        "probe_gateways",
        lambda: [
            GatewayCandidate(
                "127.0.0.1", 20128, "http://127.0.0.1:20128", True, "omniroute", "OmniRoute"
            ),
            GatewayCandidate(
                "127.0.0.1", 20129, "http://127.0.0.1:20129", True, "9router", "9router"
            ),
        ],
    )

    cli.cmd_detect(output_json=True)
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["gateways"]) == 2
    assert payload["gateways"][0]["identity"] == "omniroute"
    assert payload["gateways"][0]["health_ok"] is True
    assert "Multiple gateways found" in payload["message"]

    cli.cmd_detect()
    assert "Multiple gateways found" in capsys.readouterr().out


def test_cmd_detect_reports_no_gateway_found(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """T020: no healthy gateway yields an explicit message and exit code 0."""
    import verdict.provider_detection as provider_detection

    monkeypatch.setattr(provider_detection, "detect_all_providers", lambda: DetectionResult())
    monkeypatch.setattr(provider_detection, "probe_gateways", lambda: [])

    cli.cmd_detect(output_json=True)
    payload = json.loads(capsys.readouterr().out)
    assert payload["gateways"] == []
    assert payload["message"] == "No local gateway found on ports 20128, 20129, 20132."

    cli.cmd_detect()
    out = capsys.readouterr().out
    assert "No local gateway found on ports 20128, 20129, 20132." in out
    assert "omniroute serve" in out


def test_cmd_detect_exits_nonzero_on_detection_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    import verdict.provider_detection as provider_detection

    def fail() -> DetectionResult:
        raise RuntimeError("boom")

    monkeypatch.setattr(provider_detection, "detect_all_providers", fail)

    with pytest.raises(SystemExit) as exc:
        cli.cmd_detect()

    assert exc.value.code == 1


def test_cmd_benchmark_rejects_live_provider_without_explicit_opt_in() -> None:
    with pytest.raises(ValueError, match="explicitly enabled"):
        cli.cmd_benchmark(
            "benchmarks/fixtures/reproducible.json",
            allow_live_provider=False,
            live_provider="openai/gpt-4o",
        )


def test_main_dispatches_help_route_stats_detect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli.sys, "argv", ["verdict"])
    cli.main()
    assert "Available commands" in capsys.readouterr().out

    # Pin an empty provider catalog so the route falls back to the primary
    # model deterministically even on hosts running a live local server.
    cfg_dir = tmp_path / ".config" / "verdict"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "verdict.yaml").write_text(
        "primary_model: anthropic/claude-opus-5\nproviders: {}\nlog_path: ''\n"
    )
    monkeypatch.setattr(cli.sys, "argv", ["verdict", "route", "hello", "--terse"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    route_payload = json.loads(capsys.readouterr().out)
    assert route_payload["model"] == "anthropic/claude-opus-5"
    assert route_payload["transport_outcome"] == "error"

    monkeypatch.setattr(
        cli.sys, "argv", ["verdict", "stats", "--log_path", str(tmp_path / "missing.jsonl")]
    )
    cli.main()
    assert "No log file found" in capsys.readouterr().out

    import verdict.provider_detection as provider_detection

    monkeypatch.setattr(provider_detection, "detect_all_providers", lambda: DetectionResult())
    monkeypatch.setattr(cli.sys, "argv", ["verdict", "detect", "--json"])
    cli.main()
    assert '"local_servers"' in capsys.readouterr().out


def test_cmd_benchmark_writes_json_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output_path = tmp_path / "benchmark-report.json"
    fixture_path = Path(__file__).parent.parent / "benchmarks" / "fixtures" / "reproducible.json"

    cli.cmd_benchmark(str(fixture_path), str(output_path))

    out = capsys.readouterr().out
    assert "mode: local-reproducible" in out
    payload = json.loads(output_path.read_text())
    assert payload["fixture_path"] == str(fixture_path)
    assert payload["live_provider"] is None


def test_main_dispatches_benchmark_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    output_path = tmp_path / "benchmark.json"
    monkeypatch.setattr(
        cli.sys, "argv", ["verdict", "benchmark", "--output-json", str(output_path)]
    )

    cli.main()

    assert "compatibility_routing" in capsys.readouterr().out
    assert output_path.exists()


def test_cmd_setup_auto_and_sync_mock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # mock config / home folders
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))

    # define mocks
    from verdict.provider_detection import DetectedProvider, DetectionResult

    result = DetectionResult(
        local_servers=[
            DetectedProvider(
                id="ollama",
                name="Ollama",
                type="local_server",
                base_url="http://localhost:11434/v1",
                models=["llama3"],
                server_running=True,
            )
        ]
    )

    # mock detect_all_providers
    import verdict.provider_detection as provider_detection

    monkeypatch.setattr(provider_detection, "detect_all_providers", lambda: result)

    # Mock inputs mock-up:
    # 1. should_auto: yes ("y")
    # 2. selected_option: ollama ("1")
    # 3. selected_model: llama3 ("1")
    # 4. Sync prompt: yes ("y")
    # 5. Fallback prompt: no ("n")
    inputs = ["y", "1", "1", "y", "n"]

    def mock_ask(*args, **kwargs):
        if inputs:
            return inputs.pop(0)
        return ""

    monkeypatch.setattr(cli.Prompt, "ask", mock_ask)

    # mock api requests
    posted_nodes = []

    def mock_api_request(method, path, body=None):
        if method == "GET" and path == "/api/provider-nodes":
            return {"items": []}  # no existing nodes
        elif method == "POST" and path == "/api/provider-nodes":
            posted_nodes.append(body)
            return {"ok": True}
        return None

    monkeypatch.setattr(cli, "_omniroute_api_request", mock_api_request)

    cli.cmd_setup()

    # Assertions
    assert len(posted_nodes) == 1

    assert posted_nodes[0]["provider"] == "ollama"
    assert posted_nodes[0]["baseUrl"] == "http://localhost:11434/v1"

    # Verify verdict config file was written
    cfg_file = tmp_path / ".config" / "verdict" / "verdict.yaml"
    assert cfg_file.exists()
    import yaml

    with open(cfg_file) as f:
        cfg = yaml.safe_load(f)
    assert cfg["primary_model"] == "llama3"
    assert cfg["providers"]["ollama"]["base_url"] == "http://localhost:11434/v1"


def test_cmd_setup_wires_detected_gateway_into_config_and_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T009/T010: a healthy detected gateway is written to config and env."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.delenv("OMNIROUTE_BASE_URL", raising=False)

    import verdict.provider_detection as provider_detection
    from verdict.provider_detection import DetectedProvider, DetectionResult, GatewayCandidate

    fake_result = DetectionResult(
        centralized_routers=[
            DetectedProvider(
                id="omniroute",
                name="OmniRoute",
                type="centralized_router",
                base_url="http://127.0.0.1:20128/v1",
                server_running=True,
            )
        ]
    )
    monkeypatch.setattr(provider_detection, "detect_all_providers", lambda: fake_result)
    monkeypatch.setattr(
        provider_detection,
        "probe_gateways",
        lambda ports=None: [
            GatewayCandidate(
                host="127.0.0.1",
                port=20128,
                url="http://127.0.0.1:20128",
                health_ok=True,
                identity="omniroute",
                display_name="OmniRoute",
            )
        ],
    )

    inputs = ["ollama", "1", "n"]

    def mock_ask(*args, **kwargs):
        if inputs:
            return inputs.pop(0)
        return kwargs.get("default", "")

    monkeypatch.setattr(cli.Prompt, "ask", mock_ask)
    monkeypatch.setattr(cli, "_omniroute_api_request", lambda *a, **k: None)

    cli.cmd_setup()

    assert os.environ.get("OMNIROUTE_BASE_URL") is None
    config_path = tmp_path / ".config" / "verdict" / "verdict.yaml"
    assert config_path.exists()
    saved = yaml.safe_load(config_path.read_text())
    assert saved["gateway_url"] == "http://127.0.0.1:20128"


def test_cmd_setup_prompts_on_malformed_existing_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T011: malformed existing verdict.yaml prompts before overwrite; 'n' aborts."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))

    config_dir = tmp_path / ".config" / "verdict"
    config_dir.mkdir(parents=True)
    (config_dir / "verdict.yaml").write_text("not: valid: yaml: [")

    monkeypatch.setattr(cli.Prompt, "ask", lambda *a, **k: "n")

    with pytest.raises(SystemExit):
        cli.cmd_setup()


def test_cmd_doctor_all_healthy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))

    cfg_dir = tmp_path / ".config" / "verdict"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "verdict.yaml").write_text(
        "schema_version: 1\n"
        "primary_model: anthropic/claude-opus-5\n"
        "log_path: route-log.jsonl\n"
        "gateway_url: http://localhost:11434/v1\n"
        "providers:\n"
        "  ollama:\n"
        "    base_url: http://localhost:11434/v1\n"
    )

    # Mock omniroute API helper - healthy, no duplicates
    def mock_api_request(method, path, body=None):
        if method == "GET" and path == "/api/provider-nodes":
            return [{"id": "node1", "name": "Ollama", "baseUrl": "http://127.0.0.1:11434/v1"}]
        return None

    monkeypatch.setattr(cli, "_omniroute_api_request", mock_api_request)

    # Mock gateway health probe (T015) so the configured gateway_url reports healthy.
    import urllib.request

    class _FakeHealthResp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _FakeHealthResp())

    import verdict.documentation_preflight as documentation_preflight

    monkeypatch.setattr(documentation_preflight, "discover_sources", lambda _root=None: ())

    # Mock socket connection to make local port reachable
    import socket
    from unittest.mock import MagicMock

    def mock_create_connection(address, timeout=None, source_address=None):
        return MagicMock()

    monkeypatch.setattr(socket, "create_connection", mock_create_connection)

    # Satisfy required credentials so doctor reports healthy
    monkeypatch.setenv("OMNIROUTE_API_KEY", "fake-key-for-test")
    monkeypatch.setenv("OMNIROUTE_BASE_URL", "http://localhost:0")

    # A healthy host also has the memory-bridge state: text mode now runs the
    # same shared collector as --json, including run_doctor_diagnostics.
    (tmp_path / ".verdict").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".verdict" / "memory.db").touch()
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".mcp.json").write_text('{"mcpServers": {}}', encoding="utf-8")

    cli.cmd_doctor()

    out = capsys.readouterr().out
    assert "System is healthy! All checks passed." in out
    assert "Doctor Report: 0 issues identified. 0 resolved." in out


def test_cli_documentation_json_surfaces_blocked_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import sys

    monkeypatch.setattr(cli, "_omniroute_api_request", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "_read_omniroute_token", lambda: None)
    monkeypatch.setattr(cli, "console", cli.Console(quiet=True))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "verdict",
            "memory",
            "docs",
            "--json",
            "--repo-root",
            str(tmp_path),
            "--db-path",
            str(tmp_path / "memory.db"),
        ],
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    report = json.loads(capsys.readouterr().out)
    assert report["operation"] == "documentation-preflight"
    assert report["status"] == "blocked"


def test_cli_runtime_plan_is_json_and_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import sys

    state_dir = tmp_path / "runtime"
    monkeypatch.setenv("VERDICT_RUNTIME_STATE_DIR", str(state_dir))
    import verdict.runtime_daemons as runtime_daemons

    monkeypatch.setattr(runtime_daemons, "_probe_endpoint", lambda _endpoint: False)
    monkeypatch.setattr(runtime_daemons, "_probe_health", lambda _endpoint: "unavailable")

    class EmptyInspector:
        def snapshots(self) -> tuple[object, ...]:
            return ()

        def snapshot(self, _pid: int) -> None:
            return None

    monkeypatch.setattr(runtime_daemons, "ProcfsInspector", lambda: EmptyInspector())
    monkeypatch.setattr(sys, "argv", ["verdict", "runtime", "reconcile", "--plan", "--json"])

    cli.main()

    report = json.loads(capsys.readouterr().out)
    assert report["operation"] == "runtime"
    assert report["contract_version"] == "1"
    # Read-only status must not create ownership state. The command may create
    # no directory at all, or the test environment may create an empty parent.
    assert not any(state_dir.glob("*.ownership.json"))


def test_cli_runtime_apply_requires_explicit_consent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import sys

    monkeypatch.setattr(
        sys,
        "argv",
        ["verdict", "runtime", "reconcile", "--apply", "--service", "ruflo-mcp", "--json"],
    )

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 2
    assert "explicit consent" in capsys.readouterr().out


def test_cli_memory_docs_json_reports_repaired_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import sys

    import verdict.documentation_preflight as documentation_preflight

    docs = tmp_path / "docs" / "adr"
    docs.mkdir(parents=True)
    (docs / "ADR-001.md").write_text("# Decision\n\nUse verified docs.", encoding="utf-8")
    source = documentation_preflight.DocumentationSource(
        "fixture-cli",
        "fixture",
        "https://example.test/fixture",
        "commit-1",
        tmp_path,
        freshness_seconds=10**12,
    )
    monkeypatch.setattr(documentation_preflight, "discover_sources", lambda _root=None: (source,))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "verdict",
            "memory",
            "docs",
            "--json",
            "--fix",
            "--repo-root",
            str(tmp_path),
            "--db-path",
            str(tmp_path / "memory.db"),
        ],
    )
    cli.main()
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "ready"
    assert report["state"] == "repaired"
    assert report["repaired"] is True


def _doctor_healthy_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Shared fixture: a fully healthy host for doctor exit-code tests."""
    cfg_dir = tmp_path / ".config" / "verdict"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "verdict.yaml").write_text(
        "schema_version: 1\n"
        "primary_model: anthropic/claude-opus-5\n"
        "log_path: route-log.jsonl\n"
        "gateway_url: http://localhost:11434/v1\n"
        "providers:\n"
        "  ollama:\n"
        "    base_url: http://localhost:11434/v1\n"
    )

    def mock_api_request(method, path, body=None):
        if method == "GET" and path == "/api/provider-nodes":
            return [{"id": "node1", "name": "Ollama", "baseUrl": "http://127.0.0.1:11434/v1"}]
        return None

    monkeypatch.setattr(cli, "_omniroute_api_request", mock_api_request)

    import urllib.request

    class _FakeHealthResp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _FakeHealthResp())

    import verdict.documentation_preflight as documentation_preflight

    monkeypatch.setattr(documentation_preflight, "discover_sources", lambda _root=None: ())

    import socket
    from unittest.mock import MagicMock

    monkeypatch.setattr(
        socket, "create_connection", lambda address, timeout=None, source_address=None: MagicMock()
    )

    monkeypatch.setenv("OMNIROUTE_API_KEY", "fake-key-for-test")
    monkeypatch.setenv("OMNIROUTE_BASE_URL", "http://localhost:0")

    # Pre-populate the memory-bridge state so run_doctor_diagnostics reports
    # no issues either.
    verdict_dir = tmp_path / ".verdict"
    verdict_dir.mkdir(parents=True, exist_ok=True)
    (verdict_dir / "memory.db").touch()


def test_cli_doctor_json_healthy_host_exits_zero_with_ok_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """(i) A healthy host must exit 0 in --json mode with status "ok".

    This replaces the old test_cli_doctor_json_has_machine_readable_report,
    which enshrined the bug where run_doctor_diagnostics never returns
    "healthy" (only "ok"/"issues_found"), so --json always exited 1.
    """
    import sys

    _doctor_healthy_fixture(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".mcp.json").write_text('{"mcpServers": {}}', encoding="utf-8")

    monkeypatch.setattr(sys, "argv", ["verdict", "doctor", "--json"])
    cli.main()
    report = json.loads(capsys.readouterr().out)
    assert report["documentation_preflight"]["status"] == "ready"
    assert report["status"] == "ok"
    assert report["issues"] == []
    assert report["warnings"] == []


def test_cli_doctor_json_missing_mcp_config_warns_but_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """(iv) No .mcp.json is a non-fatal warning, not an issue: exit 0."""
    import sys

    _doctor_healthy_fixture(tmp_path, monkeypatch)
    cwd_dir = tmp_path / "no-mcp-cwd"
    cwd_dir.mkdir()
    monkeypatch.chdir(cwd_dir)
    assert not (cwd_dir / ".mcp.json").exists()

    monkeypatch.setattr(sys, "argv", ["verdict", "doctor", "--json"])
    cli.main()
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "ok"
    assert report["issues"] == []
    assert "missing_mcp_config" in report["warnings"]


def test_cli_doctor_missing_required_credential_exits_one_both_modes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """(ii) A missing required credential must exit 1 in text AND --json mode."""
    import sys

    _doctor_healthy_fixture(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".mcp.json").write_text('{"mcpServers": {}}', encoding="utf-8")
    monkeypatch.delenv("OMNIROUTE_API_KEY", raising=False)

    monkeypatch.setattr(sys, "argv", ["verdict", "doctor", "--json"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "issues_found"
    assert any("OMNIROUTE_API_KEY" in issue for issue in report["issues"])

    with pytest.raises(SystemExit) as exc:
        cli.cmd_doctor()
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "Required credential OMNIROUTE_API_KEY is not set" in out


def test_cli_doctor_corrupt_config_exits_one_both_modes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """(iii) Corrupt verdict.yaml must exit 1 in text AND --json mode."""
    import sys

    _doctor_healthy_fixture(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".mcp.json").write_text('{"mcpServers": {}}', encoding="utf-8")

    cfg_dir = tmp_path / ".config" / "verdict"
    (cfg_dir / "verdict.yaml").write_text("not: valid: yaml: [")

    monkeypatch.setattr(sys, "argv", ["verdict", "doctor", "--json"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "issues_found"
    assert any("corrupted/invalid YAML" in issue for issue in report["issues"])

    with pytest.raises(SystemExit) as exc:
        cli.cmd_doctor()
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "Configuration file is corrupted/invalid YAML" in out


def test_cli_doctor_fix_creates_mcp_config_and_reports_repaired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """(v) --fix still creates .mcp.json and records it in `repaired`."""
    import sys

    _doctor_healthy_fixture(tmp_path, monkeypatch)
    cwd_dir = tmp_path / "fix-cwd"
    cwd_dir.mkdir()
    monkeypatch.chdir(cwd_dir)
    assert not (cwd_dir / ".mcp.json").exists()

    monkeypatch.setattr(sys, "argv", ["verdict", "doctor", "--json", "--fix"])
    cli.main()
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "ok"
    assert "created_mcp_config" in report["repaired"]
    assert (cwd_dir / ".mcp.json").exists()


def test_cli_doctor_network_rate_limited_preflight_warns_but_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """(a) A documentation preflight blocked purely by third-party GitHub
    rate limiting/network errors must not fail doctor in either mode."""
    import sys

    import verdict.documentation_preflight as documentation_preflight

    _doctor_healthy_fixture(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".mcp.json").write_text('{"mcpServers": {}}', encoding="utf-8")

    blocked_report = documentation_preflight.DocumentationPreflightReport(
        status="blocked",
        sources=2,
        inventory=10,
        ingested=0,
        skipped_fresh=0,
        stale=0,
        missing=0,
        unverifiable=2,
        errors=(
            "ruflo:resolve:ValueError:authoritative fetch failed: "
            "HTTP Error 403: rate limit exceeded",
            "ruvector:resolve:ValueError:authoritative fetch failed: "
            "HTTP Error 403: rate limit exceeded",
        ),
    )
    monkeypatch.setattr(
        documentation_preflight, "run_documentation_preflight", lambda **kwargs: blocked_report
    )

    monkeypatch.setattr(sys, "argv", ["verdict", "doctor", "--json"])
    cli.main()
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "ok"
    assert report["issues"] == []
    assert any("preflight" in w for w in report["warnings"])

    cli.cmd_doctor()
    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "System is healthy! All checks passed." in out


def test_cli_doctor_non_network_preflight_failure_exits_one_both_modes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """(b) A genuinely stale/missing local documentation set (no network
    error) must still fail doctor in both modes."""
    import sys

    import verdict.documentation_preflight as documentation_preflight

    _doctor_healthy_fixture(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".mcp.json").write_text('{"mcpServers": {}}', encoding="utf-8")

    blocked_report = documentation_preflight.DocumentationPreflightReport(
        status="blocked",
        sources=1,
        inventory=5,
        ingested=0,
        skipped_fresh=0,
        stale=2,
        missing=3,
        unverifiable=0,
        errors=(
            "adr:implementation/adrs/ADR-001.md:ValueError:"
            "source blob SHA does not match fetched content",
        ),
    )
    monkeypatch.setattr(
        documentation_preflight, "run_documentation_preflight", lambda **kwargs: blocked_report
    )

    monkeypatch.setattr(sys, "argv", ["verdict", "doctor", "--json"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "issues_found"
    assert any("did not pass" in issue for issue in report["issues"])

    with pytest.raises(SystemExit) as exc:
        cli.cmd_doctor()
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "authoritative documentation preflight did not pass" in out


def test_cmd_doctor_issues_and_duplicates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))

    # Config with issue: literal API key in URL, and duplicate base_url
    cfg_dir = tmp_path / ".config" / "verdict"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "verdict.yaml").write_text(
        "primary_model: anthropic/claude-opus-5\n"
        "log_path: route-log.jsonl\n"
        "providers:\n"
        "  ollama:\n"
        "    base_url: http://localhost:11434/v1/sk-testkey\n"
        "  ollama2:\n"
        "    base_url: http://localhost:11434/v1/sk-testkey\n"
    )

    # Mock duplicate nodes returned from OmniRoute API, clean_url is identical
    deleted_nodes = []

    def mock_api_request(method, path, body=None):
        if method == "GET" and path == "/api/provider-nodes":
            return [
                {"id": "node1", "name": "Ollama1", "baseUrl": "http://127.0.0.1:11434/v1"},
                {"id": "node2", "name": "Ollama2", "baseUrl": "http://127.0.0.1:11434/v1"},
            ]
        elif method == "DELETE" and path.startswith("/api/provider-nodes/"):
            deleted_nodes.append(path.split("/")[-1])
            return {"ok": True}
        return None

    monkeypatch.setattr(cli, "_omniroute_api_request", mock_api_request)

    # Mock user prompt answers "y"
    monkeypatch.setattr(cli.Prompt, "ask", lambda *args, **kwargs: "y")

    # Mock socket check as always failing (unreachable) to cause host offline issue
    import socket

    def mock_create_connection(address, timeout=None, source_address=None):
        raise OSError("offline")

    monkeypatch.setattr(socket, "create_connection", mock_create_connection)

    # Real, unresolved issues (literal secret in config, duplicate URL,
    # offline nodes) must make text-mode doctor exit non-zero, matching
    # --json behavior for the same inputs.
    with pytest.raises(SystemExit) as exc:
        cli.cmd_doctor()
    assert exc.value.code == 1

    out = capsys.readouterr().out
    assert "Literal API key detected inside the host URL for provider" in out
    assert "Duplicate host URL configured in verdict.yaml" in out
    assert "Duplicate node 'Ollama2'" in out
    assert "node2" in deleted_nodes


def test_cmd_doctor_flags_legacy_config_filename_and_offers_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """T016: config.yaml (not verdict.yaml) is flagged, and --fix renames it."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.delenv("OMNIROUTE_BASE_URL", raising=False)

    cfg_dir = tmp_path / ".config" / "verdict"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.yaml").write_text("primary_model: gpt-4\nproviders: {}\n")

    monkeypatch.setattr(cli, "_omniroute_api_request", lambda *a, **k: None)

    # This fixture also leaves other unrelated issues unresolved (no gateway
    # URL, missing required credential), so both calls exit non-zero.
    with pytest.raises(SystemExit) as exc:
        cli.cmd_doctor()
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "must be 'verdict.yaml'" in out
    assert (cfg_dir / "config.yaml").exists()
    assert not (cfg_dir / "verdict.yaml").exists()

    with pytest.raises(SystemExit) as exc:
        cli.cmd_doctor(fix=True)
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "Renamed" in out
    assert not (cfg_dir / "config.yaml").exists()
    assert (cfg_dir / "verdict.yaml").exists()


def test_cmd_doctor_flags_invalid_env_var_formats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """T017: malformed OMNIROUTE_BASE_URL / OPENAI_API_KEY are flagged."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setenv("OMNIROUTE_BASE_URL", "http://localhost:20128/")
    monkeypatch.setenv("OPENAI_API_KEY", "not-a-valid-key")
    monkeypatch.setattr(cli, "_omniroute_api_request", lambda *a, **k: None)

    import urllib.request
    from urllib.error import URLError

    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *a, **k: (_ for _ in ()).throw(URLError("connection refused")),
    )

    # Malformed env vars plus missing config/gateway are real, unresolved
    # issues, so text-mode doctor must exit non-zero.
    with pytest.raises(SystemExit) as exc:
        cli.cmd_doctor()
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "OMNIROUTE_BASE_URL has invalid format" in out
    assert "OPENAI_API_KEY appears invalid" in out


def test_cmd_doctor_flags_missing_schema_version_and_fixes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """T023: config without schema_version is flagged; --fix migrates it."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.delenv("OMNIROUTE_BASE_URL", raising=False)

    cfg_dir = tmp_path / ".config" / "verdict"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "verdict.yaml").write_text("primary_model: gpt-4\nproviders: {}\n")
    monkeypatch.setattr(cli, "_omniroute_api_request", lambda *a, **k: None)

    # This fixture also leaves other unrelated issues unresolved (no gateway
    # URL, missing required credential), so both calls exit non-zero.
    with pytest.raises(SystemExit) as exc:
        cli.cmd_doctor()
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "older Verdict version" in out

    with pytest.raises(SystemExit) as exc:
        cli.cmd_doctor(fix=True)
    assert exc.value.code == 1
    saved = yaml.safe_load((cfg_dir / "verdict.yaml").read_text())
    assert saved["schema_version"] == 1


def test_cmd_doctor_prints_env_example_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """T024: doctor always points users at .env.example."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setattr(cli, "_omniroute_api_request", lambda *a, **k: None)

    # No config file / gateway / credentials in this fixture are real,
    # unresolved issues, so doctor exits non-zero even though the
    # .env.example pointer is still shown.
    with pytest.raises(SystemExit) as exc:
        cli.cmd_doctor()
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert ".env.example" in out


# --- Shared-collector parity (review R3 cases A-F) and classifier tightening ---

_PARITY_CFG_OK = (
    "schema_version: 1\n"
    "primary_model: anthropic/claude-opus-5\n"
    "log_path: route-log.jsonl\n"
    "gateway_url: http://localhost:11434/v1\n"
    "providers:\n"
    "  ollama:\n"
    "    base_url: http://localhost:11434/v1\n"
)


def _doctor_parity_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    cfg: str = _PARITY_CFG_OK,
    memdb: bool = True,
    gateway_ok: bool = True,
    doc_report: object | None = None,
) -> None:
    import socket
    import urllib.request
    from unittest.mock import MagicMock

    import verdict.documentation_preflight as documentation_preflight

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    cfg_dir = tmp_path / ".config" / "verdict"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "verdict.yaml").write_text(cfg)
    monkeypatch.setattr(
        cli,
        "_omniroute_api_request",
        lambda m, p, body=None: (
            [{"id": "n1", "name": "Ollama", "baseUrl": "http://127.0.0.1:11434/v1"}]
            if (m, p) == ("GET", "/api/provider-nodes")
            else None
        ),
    )

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

    def _urlopen(*a, **k):
        if not gateway_ok:
            raise OSError("connection refused")
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    monkeypatch.setattr(documentation_preflight, "discover_sources", lambda _root=None: ())
    if doc_report is not None:
        monkeypatch.setattr(
            documentation_preflight, "run_documentation_preflight", lambda **k: doc_report
        )
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: MagicMock())
    monkeypatch.setenv("OMNIROUTE_API_KEY", "fake-key-for-test")
    monkeypatch.setenv("OMNIROUTE_BASE_URL", "http://localhost:0")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    if memdb:
        (tmp_path / ".verdict").mkdir(parents=True, exist_ok=True)
        (tmp_path / ".verdict" / "memory.db").touch()
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".mcp.json").write_text('{"mcpServers": {}}', encoding="utf-8")


def _doctor_both_modes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> tuple[int, int, dict[str, Any]]:
    import sys

    monkeypatch.setattr(sys, "argv", ["verdict", "doctor", "--json"])
    try:
        cli.main()
        json_exit = 0
    except SystemExit as exc:
        json_exit = int(exc.code or 0)
    report = json.loads(capsys.readouterr().out)
    try:
        cli.cmd_doctor()
        text_exit = 0
    except SystemExit as exc:
        text_exit = int(exc.code or 0)
    capsys.readouterr()
    return json_exit, text_exit, report


def _blocked_doc_report(*, missing: int, stale: int, errors: tuple[str, ...]) -> object:
    import verdict.documentation_preflight as documentation_preflight

    return documentation_preflight.DocumentationPreflightReport(
        status="blocked",
        sources=2,
        inventory=missing + stale,
        ingested=0,
        skipped_fresh=0,
        stale=stale,
        missing=missing,
        unverifiable=1,
        errors=errors,
    )


@pytest.mark.parametrize(
    ("case", "host", "expect_exit", "issue_substr", "warning_substr"),
    [
        (
            "A-schema-version-missing",
            {"cfg": _PARITY_CFG_OK.replace("schema_version: 1\n", "")},
            1,
            "older Verdict version",
            None,
        ),
        ("B-memory-db-missing", {"memdb": False}, 1, "missing_memory_db", None),
        ("C-gateway-unreachable", {"gateway_ok": False}, 1, "Gateway unreachable", None),
        (
            "D-no-primary-model-literal-key",
            {"cfg": "schema_version: 1\nproviders:\n  x:\n    base_url: http://h/sk-abc\n"},
            1,
            "Literal API key detected",
            None,
        ),
        (
            "E-missing-docs-with-one-rate-limit",
            {
                "doc_report": _blocked_doc_report(
                    missing=800,
                    stale=40,
                    errors=(
                        "ruvector:resolve:ValueError:authoritative fetch failed: "
                        "HTTP Error 403: rate limit exceeded",
                    ),
                )
            },
            1,
            "did not pass",
            None,
        ),
        (
            "F-forbidden-inventory",
            {
                "doc_report": _blocked_doc_report(
                    missing=0,
                    stale=0,
                    errors=("adr:inventory:HTTPError:HTTP Error 403: Forbidden",),
                )
            },
            1,
            "did not pass",
            None,
        ),
        (
            "G-rate-limited-403-only",
            {
                "doc_report": _blocked_doc_report(
                    missing=0,
                    stale=0,
                    errors=(
                        "ruflo:resolve:ValueError:authoritative fetch failed: "
                        "HTTP Error 403: rate limit exceeded",
                    ),
                )
            },
            0,
            None,
            "preflight unreachable",
        ),
        ("healthy", {}, 0, None, None),
    ],
)
def test_cli_doctor_text_and_json_share_one_collector(
    case: str,
    host: dict[str, Any],
    expect_exit: int,
    issue_substr: str | None,
    warning_substr: str | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Text and --json modes must agree on exit and classification (review R3)."""
    _doctor_parity_host(tmp_path, monkeypatch, **host)
    json_exit, text_exit, report = _doctor_both_modes(monkeypatch, capsys)
    assert json_exit == text_exit == expect_exit, case
    assert report["status"] == ("issues_found" if expect_exit else "ok")
    if issue_substr is None:
        assert report["issues"] == []
    else:
        assert any(issue_substr in issue for issue in report["issues"]), report["issues"]
    if warning_substr is not None:
        assert any(warning_substr in w for w in report["warnings"]), report["warnings"]


@pytest.mark.parametrize(
    ("missing", "stale", "orphaned", "errors", "expected"),
    [
        # E: real doc gaps are never masked by one network error.
        (800, 40, 0, ("r:resolve:ValueError:HTTP Error 403: rate limit exceeded",), False),
        # F: a bare 403 is auth/permission, not a rate limit.
        (0, 0, 0, ("adr:inventory:HTTPError:HTTP Error 403: Forbidden",), False),
        # G: a 403 with rate-limit text and no doc gap is network-only.
        (0, 0, 0, ("r:resolve:ValueError:HTTP Error 403: rate limit exceeded",), True),
        (0, 0, 0, ("r:resolve:HTTPError:HTTP Error 429: Too Many Requests",), True),
        (0, 0, 0, ("r:inventory:URLError:<urlopen error timed out>",), True),
        (0, 0, 0, ("r:inventory:URLError:[Errno 111] Connection refused",), True),
        (0, 0, 0, ("r:resolve:URLError:Name or service not known",), True),
        (0, 0, 1, ("r:resolve:HTTPError:HTTP Error 429: Too Many Requests",), False),
        (0, 0, 0, (), False),
        # Non-fetch error (content integrity) is never network-only.
        (0, 0, 0, ("adr:implementation/adrs/ADR-001.md:ValueError:timed out",), False),
        (
            0,
            0,
            0,
            (
                "r:resolve:HTTPError:HTTP Error 429: Too Many Requests",
                "adr:inventory:HTTPError:HTTP Error 403: Forbidden",
            ),
            False,
        ),
    ],
)
def test_doctor_documentation_preflight_network_only_classifier(
    missing: int, stale: int, orphaned: int, errors: tuple[str, ...], expected: bool
) -> None:
    import verdict.documentation_preflight as documentation_preflight

    report = documentation_preflight.DocumentationPreflightReport(
        status="blocked",
        sources=1,
        inventory=missing + stale,
        ingested=0,
        skipped_fresh=0,
        stale=stale,
        missing=missing,
        unverifiable=1,
        errors=errors,
        orphaned=orphaned,
    )
    assert cli._doctor_documentation_preflight_is_network_only_failure(report) is expected


def test_serve_dev_flag_enables_reload_and_dev_profile(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """T022: `verdict serve --dev` enables hot reload and the dev profile."""
    import verdict.api as api_module

    calls: list[dict] = []
    monkeypatch.setattr(
        api_module,
        "start_server",
        lambda port, host, reload=False: calls.append(
            {"port": port, "host": host, "reload": reload}
        ),
    )
    monkeypatch.delenv("LLMGATE_AVAILABILITY_PROFILE", raising=False)
    monkeypatch.setattr("sys.argv", ["verdict", "serve", "--dev"])

    cli.main()

    assert calls == [{"port": 8000, "host": None, "reload": True}]
    assert os.environ.get("LLMGATE_AVAILABILITY_PROFILE") == "development"
    assert "hot-reload enabled" in capsys.readouterr().out


def test_cmd_catalog_fetches_and_reconciles_both_projections(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    public = {"object": "list", "data": [{"id": "a/model", "owned_by": "a"}]}
    management = {
        "catalogVersion": "model-metadata-v1:static",
        "catalog": {"a": {"active": True, "models": [{"id": "a/model"}]}},
    }

    class Response:
        def __init__(self, payload: object) -> None:
            self.payload = json.dumps(payload).encode()

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self) -> bytes:
            return self.payload

    def mock_urlopen(request: object, timeout: int) -> Response:
        url = str(request.full_url)  # type: ignore[attr-defined]
        return Response(public if url.endswith("/v1/models") else management)

    monkeypatch.setattr("urllib.request.urlopen", mock_urlopen)
    cli.cmd_catalog(
        base_url="https://example.test",
        management=False,
        expected_rows=1,
        freshness_seconds=3600,
        db_path=None,
        probe=False,
        probe_limit=1,
        probe_timeout=1.0,
        output_json=True,
    )
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "qualified"
    assert output["projection_reconciliation"]["passed"] is True


def test_cmd_catalog_fetch_timeout_is_named_and_not_a_pass(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    def mock_urlopen(request: object, timeout: float) -> object:
        del request
        assert timeout >= 30
        raise TimeoutError("timed out")

    monkeypatch.setattr("urllib.request.urlopen", mock_urlopen)
    with pytest.raises(SystemExit) as exited:
        cli.cmd_catalog(
            base_url="https://example.test",
            management=True,
            expected_rows=0,
            freshness_seconds=3600,
            db_path=None,
            probe=False,
            probe_limit=1,
            probe_timeout=1.0,
            output_json=True,
        )
    assert exited.value.code == 1
    output = json.loads(capsys.readouterr().out)
    assert output["passed"] is False
    assert output["status"] == "unknown"
    assert "catalog_fetch_timeout" in output["errors"]
    assert "TimeoutError" in output["errors"]


def test_cmd_hook_claude_gate_exits_2_when_catalog_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    import argparse

    def mock_urlopen(request: object, timeout: float) -> object:
        del request, timeout
        raise TimeoutError("timed out")

    monkeypatch.setattr("urllib.request.urlopen", mock_urlopen)
    args = argparse.Namespace(hook_command="claude-gate", base_url="http://127.0.0.1:20128")
    with pytest.raises(SystemExit) as exited:
        cli.cmd_hook(args)
    assert exited.value.code == 2


def test_cmd_catalog_fails_closed_when_management_projection_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps({"object": "list", "data": [{"id": "a/model"}]}).encode()

    def mock_urlopen(request: object, timeout: int) -> Response:
        if str(request.full_url).endswith("/api/models/catalog"):  # type: ignore[attr-defined]
            raise OSError("management unavailable")
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", mock_urlopen)
    with pytest.raises(SystemExit):
        cli.cmd_catalog(
            base_url="https://example.test",
            management=False,
            expected_rows=1,
            freshness_seconds=3600,
            db_path=None,
            probe=False,
            probe_limit=1,
            probe_timeout=1.0,
            output_json=True,
        )
    output = json.loads(capsys.readouterr().out)
    assert output["projection_reconciliation"]["status"] == "unknown"
    assert output["projection_reconciliation"]["passed"] is False


def test_cmd_check_missing_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))

    with pytest.raises(SystemExit) as exc:
        cli.cmd_check()
    assert exc.value.code == 1
    assert "Configuration file (verdict.yaml) is missing" in capsys.readouterr().out


def test_cmd_check_valid_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))

    cfg_dir = tmp_path / ".config" / "verdict"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "verdict.yaml").write_text(
        "primary_model: anthropic/claude-opus-5\n"
        "log_path: route-log.jsonl\n"
        "providers:\n"
        "  ollama:\n"
        "    base_url: http://localhost:11434/v1\n"
    )

    cli.cmd_check()
    out = capsys.readouterr().out
    assert "Configuration file is valid" in out


def test_cmd_check_invalid_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))

    cfg_dir = tmp_path / ".config" / "verdict"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "verdict.yaml").write_text(
        "primary_model: anthropic/claude-opus-5\n"
        "log_path: route-log.jsonl\n"
        "providers:\n"
        "  ollama:\n"
        "    base_url: http://localhost:11434/v1/sk-123456\n"
    )

    with pytest.raises(SystemExit) as exc:
        cli.cmd_check()
    assert exc.value.code == 1
    assert "Literal API key detected inside host URL for provider" in capsys.readouterr().out


def test_cmd_compat_manifest_json(capsys: pytest.CaptureFixture[str]) -> None:
    cli.cmd_compat("manifest", None, True)
    output = json.loads(capsys.readouterr().out)
    assert output["schema_version"]
    assert output["manifest_hash"].startswith("sha256:")
    assert "TaskSpec" in output["contracts"]


def test_cmd_compat_check_passes_on_matching_declaration(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from verdict.compatibility_manifest import build_compatibility_manifest

    declared_path = tmp_path / "declared.json"
    declared_path.write_text(json.dumps(build_compatibility_manifest().to_dict()))

    cli.cmd_compat("check", str(declared_path), True)
    output = json.loads(capsys.readouterr().out)
    assert output["allowed"] is True


def test_cmd_compat_check_fails_closed_on_missing_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.cmd_compat("check", str(tmp_path / "nope.json"), False)
    assert exc.value.code == 1
    assert "not found" in capsys.readouterr().out


def test_cmd_compat_check_fails_closed_on_mismatch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from verdict.compatibility_manifest import build_compatibility_manifest

    manifest_dict = build_compatibility_manifest().to_dict()
    manifest_dict["contracts"]["TaskSpec"] = "sha256:" + "0" * 64
    declared_path = tmp_path / "declared.json"
    declared_path.write_text(json.dumps(manifest_dict))

    with pytest.raises(SystemExit) as exc:
        cli.cmd_compat("check", str(declared_path), True)
    assert exc.value.code == 1
    output = json.loads(capsys.readouterr().out)
    assert output["allowed"] is False
    assert "TaskSpec" in output["mismatched_contracts"]


def test_cmd_compat_check_requires_declared_arg(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.cmd_compat("check", None, False)
    assert exc.value.code == 1


def test_cmd_probe_reports_live_model(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """cmd_probe should report a live model when the transport returns 2xx."""

    def fake_transport_factory(base_url, api_key=None, opener=None):  # type: ignore[no-untyped-def]
        def transport(model_id, payload, timeout):  # type: ignore[no-untyped-def]
            assert payload["max_tokens"] == 1
            return {
                "status_code": 200,
                "body": {
                    "choices": [{"message": {"role": "assistant", "content": "OK"}}],
                    "usage": {"total_tokens": 3},
                },
            }

        return transport

    monkeypatch.setattr("verdict.probes.openai_probe_transport", fake_transport_factory)
    cli.cmd_probe(
        ["some/model:free"],
        base_url="http://localhost:20128/v1",
        output_json=True,
        allow_live_probe=True,
    )
    out = json.loads(capsys.readouterr().out)
    assert out["diagnostics"]["consented"] is True
    assert out["results"][0]["ok"] is True
    assert out["results"][0]["http_status"] == 200


def test_cmd_probe_flags_down_model(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """cmd_probe should flag a model whose transport raises."""

    def fake_transport_factory(base_url, api_key=None, opener=None):  # type: ignore[no-untyped-def]
        def transport(model_id, payload, timeout):  # type: ignore[no-untyped-def]
            raise TimeoutError("boom")

        return transport

    monkeypatch.setattr("verdict.probes.openai_probe_transport", fake_transport_factory)
    with pytest.raises(SystemExit):
        cli.cmd_probe(["down/model"], output_json=False, allow_live_probe=True)
    err_out = capsys.readouterr().out
    assert "DOWN" in err_out


def test_cmd_probe_requires_consent_before_live_transport(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def unexpected_transport(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("live transport must not be constructed")

    monkeypatch.setattr("verdict.probes.openai_probe_transport", unexpected_transport)
    with pytest.raises(SystemExit) as exc:
        cli.cmd_probe(["some/model"], output_json=True)
    assert exc.value.code == 2
    report = json.loads(capsys.readouterr().out)
    assert "explicit consent" in report["error"]


def test_cmd_probe_live_json_includes_sanitized_diagnostics(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_transport_factory(base_url, api_key=None, opener=None):  # type: ignore[no-untyped-def]
        def transport(model_id, payload, timeout):  # type: ignore[no-untyped-def]
            return {
                "status_code": 200,
                "body": {
                    "choices": [{"message": {"role": "assistant", "content": "OK"}}],
                    "usage": {"total_tokens": 1},
                },
            }

        return transport

    monkeypatch.setattr("verdict.probes.openai_probe_transport", fake_transport_factory)
    cli.cmd_probe(["some/model"], allow_live_probe=True, output_json=True)
    report = json.loads(capsys.readouterr().out)
    assert report["diagnostics"]["provider"] == "omniroute"
    assert report["diagnostics"]["consented"] is True
    assert report["results"][0]["ok"] is True


def test_cmd_catalog_probe_requires_consent_before_fetching_catalog_probe(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def unexpected_urlopen(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("catalog probe must not fetch without consent")

    monkeypatch.setattr("urllib.request.urlopen", unexpected_urlopen)
    with pytest.raises(SystemExit) as exc:
        cli.cmd_catalog(
            base_url="https://example.test",
            management=True,
            expected_rows=1,
            freshness_seconds=3600,
            db_path=None,
            probe=True,
            probe_limit=1,
            probe_timeout=1.0,
            output_json=True,
        )
    assert exc.value.code == 2
    report = json.loads(capsys.readouterr().out)
    assert "explicit consent" in report["error"]


def _make_documents_db(path: Path, rows: list[tuple[str, str]]) -> Path:
    import sqlite3

    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE documents (id TEXT, path TEXT, content TEXT);")
        connection.executemany(
            "INSERT INTO documents VALUES (?, ?, ?)",
            [
                (str(index), source_path, content)
                for index, (source_path, content) in enumerate(rows)
            ],
        )
    return path


def test_cmd_memory_masterdocs_default_is_unavailable_machine_readable_and_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without --allow-legacy-sqlite the migration boundary reports unavailable JSON and exits 1."""
    import argparse

    from verdict.memory_masterdocs_adapter import MasterDocsAdapter

    db = _make_documents_db(tmp_path / "MasterDocsRAG.db", [("docs/readme.md", "# Hi\n\nbody")])
    # The adapter is constructed inside cmd_memory with allowlisted_roots=(cwd,); pin cwd so the
    # fixture db is reachable. We canonicalize directly to assert the boundary outcome shape.
    monkeypatch.chdir(tmp_path)
    adapter = MasterDocsAdapter(allowlisted_roots=(tmp_path,))
    blocked = adapter.canonicalize_db_records(db, allow_legacy_sqlite=False)
    assert blocked.report.status == "unavailable"
    assert blocked.records == ()

    args = argparse.Namespace(
        memory_command="masterdocs",
        db_path=str(tmp_path / "memory.db"),
        db=str(db),
        allow_legacy_sqlite=False,
        dry_run=False,
        limit=1000,
        ingest_timestamp=None,
        json=True,
    )
    with pytest.raises(SystemExit) as exc:
        cli.cmd_memory(args)
    assert exc.value.code == 1
    out = json.loads(capsys.readouterr().out)
    assert out["report"]["status"] == "unavailable"
    assert out["records"] == []


def test_cmd_memory_masterdocs_allow_legacy_dry_run_json_canonicalizes_without_writing_plane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--allow-legacy-sqlite --dry-run --json canonicalizes a small documents fixture and writes no MemoryPlane."""
    import argparse

    from verdict.memory_plane import MemoryPlane

    db = _make_documents_db(tmp_path / "MasterDocsRAG.db", [("docs/readme.md", "# Header\n\nbody")])
    # Pin the process cwd so cmd_memory's adapter allowlist (cwd) covers the fixture.
    monkeypatch.chdir(tmp_path)
    memory_db = tmp_path / "memory.db"

    args = argparse.Namespace(
        memory_command="masterdocs",
        db_path=str(memory_db),
        db=str(db),
        allow_legacy_sqlite=True,
        dry_run=True,
        limit=1000,
        ingest_timestamp=0.0,
        json=True,
    )
    cli.cmd_memory(args)
    payload = json.loads(capsys.readouterr().out)
    assert payload["report"]["status"] == "ok"
    assert payload["report"]["status"] not in {"unavailable", "rejected", "empty"}
    assert payload["report"]["documents_accepted"] == 1
    assert payload["report"]["chunks_emitted"] == 1
    # Dry run emits canonical chunks via the report but performs no plane import.
    assert payload["report"]["chunks_emitted"] == 1
    assert payload["report"]["status"] == "ok"
    record = payload["records"][0]
    assert record["namespace"] == "masterdocs"
    assert record["trust"] == "imported-unverified"
    assert record["authority_verified"] is False

    # Dry run canonicalizes without importing any records into the configured plane.
    with MemoryPlane(str(memory_db)) as plane:
        assert plane.search("Header", namespace="masterdocs") == []


def test_cmd_memory_masterdocs_accepted_import_reports_canonical_status_and_writes_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An accepted non-dry-run import reports canonical status and persists records to MemoryPlane."""
    import argparse

    from verdict.memory_plane import MemoryPlane

    db = _make_documents_db(tmp_path / "MasterDocsRAG.db", [("docs/readme.md", "# Header\n\nbody")])
    monkeypatch.chdir(tmp_path)
    memory_db = tmp_path / "memory.db"

    args = argparse.Namespace(
        memory_command="masterdocs",
        db_path=str(memory_db),
        db=str(db),
        allow_legacy_sqlite=True,
        dry_run=False,
        limit=1000,
        ingest_timestamp=0.0,
        json=True,
    )
    cli.cmd_memory(args)
    payload = json.loads(capsys.readouterr().out)
    assert payload["report"]["status"] == "ok"
    assert payload["report"]["documents_accepted"] == 1
    assert payload["report"]["chunks_emitted"] == 1
    assert payload["report"]["ingested"] == 1

    assert memory_db.exists()
    with MemoryPlane(str(memory_db)) as plane:
        hits = plane.search("Header", namespace="masterdocs")
        assert len(hits) == 1
        assert hits[0].key.startswith("docs/readme.md#chunk-")
        assert hits[0].trust == "imported-unverified"


def test_cmd_packet_shadow_dumps_json_without_calling_eligibility_gate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from verdict.eligibility import EligibilityGate

    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("shadow dump must not call EligibilityGate")

    monkeypatch.setattr(EligibilityGate, "evaluate", boom)
    episodes = tmp_path / "episodes.json"
    episodes.write_text(
        json.dumps(
            [
                {
                    "packet_integrity_digest": "sha256:" + "33" * 32,
                    "actual_identity": "cheap/a",
                    "worker_self_report": {"outcome": "applied", "role": "advisory"},
                    "trusted_verification": {"decided": True, "role": "deciding"},
                }
            ]
        ),
        encoding="utf-8",
    )
    cli.cmd_autodev_packet_shadow(str(episodes), output_json=True)
    report = json.loads(capsys.readouterr().out)
    assert report["admission_unchanged"] is True
    assert report["labeled_from"] == "trusted_verification"
    assert report["advisory_ranking"][0]["identity"] == "cheap/a"
    assert report["advisory_ranking"][0]["wins"] == 1
    assert report["episode_count"] == 1


def test_cmd_packet_canary_is_explicit_bounded_and_rollback_restores_baseline(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from verdict.eligibility import EligibilityGate

    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("canary dump must not call EligibilityGate")

    monkeypatch.setattr(EligibilityGate, "evaluate", boom)
    digest = "sha256:" + "cd" * 32
    episodes = tmp_path / "episodes.json"
    episodes.write_text(
        json.dumps(
            [
                {
                    "packet_integrity_digest": digest,
                    "actual_identity": "b/2",
                    "trusted_verification": {"decided": True, "role": "deciding"},
                },
                {
                    "packet_integrity_digest": digest,
                    "actual_identity": "b/2",
                    "trusted_verification": {"decided": True, "role": "deciding"},
                },
                {
                    "packet_integrity_digest": digest,
                    "actual_identity": "a/1",
                    "trusted_verification": {"decided": False, "role": "deciding"},
                },
            ]
        ),
        encoding="utf-8",
    )
    admitted = tmp_path / "admitted.json"
    admitted.write_text(json.dumps(["a/1", "b/2"]), encoding="utf-8")
    cli.cmd_autodev_packet_canary(str(episodes), str(admitted), output_json=True)
    canary = json.loads(capsys.readouterr().out)
    assert canary["active"] is True
    assert canary["chosen"] == "b/2"
    assert canary["baseline"] == "a/1"
    assert canary["admission_unchanged"] is True
    state = tmp_path / "canary.json"
    state.write_text(json.dumps(canary), encoding="utf-8")
    cli.cmd_autodev_packet_canary_rollback(str(state), output_json=True)
    rolled = json.loads(capsys.readouterr().out)
    assert rolled["active"] is False
    assert rolled["chosen"] == "a/1"
    assert rolled["baseline"] == "a/1"


def test_cmd_packet_canary_refuses_missing_apply_paths(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from verdict.eligibility import EligibilityGate

    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("canary dump must not call EligibilityGate")

    monkeypatch.setattr(EligibilityGate, "evaluate", boom)
    with pytest.raises(SystemExit) as exited:
        cli.cmd_autodev_packet_canary(None, None, output_json=True)  # type: ignore[arg-type]
    assert exited.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert "episodes" in payload["error"]
