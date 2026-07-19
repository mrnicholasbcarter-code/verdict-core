"""In-process tests for CLI command helpers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from llm_gate import cli
from llm_gate.provider_detection import DetectedProvider, DetectionResult


@pytest.fixture(autouse=True)
def isolated_config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))


def test_cmd_route_terse_uses_configured_primary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg_dir = tmp_path / ".config" / "llm-gate"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "llm-gate.yaml").write_text(
        "primary_model: test-primary\n"
        "log_path: route-log.jsonl\n"
        "providers:\n"
        "  cheap:\n"
        "    base_url: http://localhost:1234/v1\n"
    )

    cli.cmd_route("deploy prod", "critical", terse=True)

    assert capsys.readouterr().out.strip() == "test-primary"


def test_cmd_route_verbose_without_config(capsys: pytest.CaptureFixture[str]) -> None:
    cli.cmd_route("format docs", "low", terse=False)

    out = capsys.readouterr().out
    assert "Routing Decision" in out
    assert "format docs" in out


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
    import llm_gate.provider_detection as provider_detection

    monkeypatch.setattr(provider_detection, "detect_all_providers", lambda: result)

    cli.cmd_detect(output_json=True)
    assert '"centralized_routers"' in capsys.readouterr().out

    cli.cmd_detect(output_config=True)
    out = capsys.readouterr().out
    assert "primary_model: router-primary" in out
    assert "9router:" in out

    cli.cmd_detect(verbose=True)
    assert "Centralized router detected" in capsys.readouterr().out


def test_cmd_detect_exits_nonzero_on_detection_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    import llm_gate.provider_detection as provider_detection

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
    monkeypatch.setattr(cli.sys, "argv", ["llm-gate"])
    cli.main()
    assert "Available commands" in capsys.readouterr().out

    monkeypatch.setattr(cli.sys, "argv", ["llm-gate", "route", "hello", "--terse"])
    cli.main()
    assert "anthropic/claude-3-opus-20240229" in capsys.readouterr().out

    monkeypatch.setattr(
        cli.sys,
        "argv",
        ["llm-gate", "stats", "--log_path", str(tmp_path / "missing.jsonl")],
    )
    cli.main()
    assert "No log file found" in capsys.readouterr().out

    import llm_gate.provider_detection as provider_detection

    monkeypatch.setattr(provider_detection, "detect_all_providers", lambda: DetectionResult())
    monkeypatch.setattr(cli.sys, "argv", ["llm-gate", "detect", "--json"])
    cli.main()
    assert '"local_servers"' in capsys.readouterr().out


def test_cmd_benchmark_writes_json_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output_path = tmp_path / "benchmark-report.json"

    cli.cmd_benchmark("benchmarks/fixtures/reproducible.json", str(output_path))

    out = capsys.readouterr().out
    assert "mode: local-reproducible" in out
    payload = json.loads(output_path.read_text())
    assert payload["fixture_path"] == "benchmarks/fixtures/reproducible.json"
    assert payload["live_provider"] is None


def test_main_dispatches_benchmark_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    output_path = tmp_path / "benchmark.json"
    monkeypatch.setattr(
        cli.sys,
        "argv",
        ["llm-gate", "benchmark", "--output-json", str(output_path)],
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
    from llm_gate.provider_detection import DetectedProvider, DetectionResult

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
    import llm_gate.provider_detection as provider_detection

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

    # Verify llm-gate config file was written
    cfg_file = tmp_path / ".config" / "llm-gate" / "llm-gate.yaml"
    assert cfg_file.exists()
    import yaml

    with open(cfg_file) as f:
        cfg = yaml.safe_load(f)
    assert cfg["primary_model"] == "llama3"
    assert cfg["providers"]["ollama"]["base_url"] == "http://localhost:11434/v1"


def test_cmd_doctor_all_healthy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))

    cfg_dir = tmp_path / ".config" / "llm-gate"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "llm-gate.yaml").write_text(
        "primary_model: anthropic/claude-3-opus-20240229\n"
        "log_path: route-log.jsonl\n"
        "providers:\n"
        "  ollama:\n"
        "    base_url: http://localhost:11434/v1\n"
    )

    # Mock omniroute API helper - healthy, no duplicates
    def mock_api_request(method, path, body=None):
        if method == "GET" and path == "/api/provider-nodes":
            return [
                {
                    "id": "node1",
                    "name": "Ollama",
                    "baseUrl": "http://127.0.0.1:11434/v1",
                }
            ]
        return None

    monkeypatch.setattr(cli, "_omniroute_api_request", mock_api_request)

    # Mock socket connection to make local port reachable
    import socket
    from unittest.mock import MagicMock

    def mock_create_connection(address, timeout=None, source_address=None):
        return MagicMock()

    monkeypatch.setattr(socket, "create_connection", mock_create_connection)

    cli.cmd_doctor()

    out = capsys.readouterr().out
    assert "System is healthy! All checks passed." in out
    assert "Doctor Report: 0 issues identified. 0 resolved." in out


def test_cmd_doctor_issues_and_duplicates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))

    # Config with issue: literal API key in URL, and duplicate base_url
    cfg_dir = tmp_path / ".config" / "llm-gate"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "llm-gate.yaml").write_text(
        "primary_model: anthropic/claude-3-opus-20240229\n"
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
                {
                    "id": "node1",
                    "name": "Ollama1",
                    "baseUrl": "http://127.0.0.1:11434/v1",
                },
                {
                    "id": "node2",
                    "name": "Ollama2",
                    "baseUrl": "http://127.0.0.1:11434/v1",
                },
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

    cli.cmd_doctor()

    out = capsys.readouterr().out
    assert "Literal API key detected inside the host URL for provider" in out
    assert "Duplicate host URL configured in llm-gate.yaml" in out
    assert "Duplicate node 'Ollama2'" in out
    assert "node2" in deleted_nodes


def test_cmd_check_missing_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))

    with pytest.raises(SystemExit) as exc:
        cli.cmd_check()
    assert exc.value.code == 1
    assert "Configuration file (llm-gate.yaml) is missing" in capsys.readouterr().out


def test_cmd_check_valid_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))

    cfg_dir = tmp_path / ".config" / "llm-gate"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "llm-gate.yaml").write_text(
        "primary_model: anthropic/claude-3-opus-20240229\n"
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

    cfg_dir = tmp_path / ".config" / "llm-gate"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "llm-gate.yaml").write_text(
        "primary_model: anthropic/claude-3-opus-20240229\n"
        "log_path: route-log.jsonl\n"
        "providers:\n"
        "  ollama:\n"
        "    base_url: http://localhost:11434/v1/sk-123456\n"
    )

    with pytest.raises(SystemExit) as exc:
        cli.cmd_check()
    assert exc.value.code == 1
    assert "Literal API key detected inside host URL for provider" in capsys.readouterr().out


def test_cmd_probe_reports_live_model(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """cmd_probe should report a live model when the transport returns 2xx."""

    def fake_transport_factory(base_url, api_key=None, opener=None):  # type: ignore[no-untyped-def]
        def transport(model_id, payload, timeout):  # type: ignore[no-untyped-def]
            assert payload["max_tokens"] == 1
            return {"status_code": 200, "body": {"usage": {"total_tokens": 3}}}

        return transport

    monkeypatch.setattr("llm_gate.probes.openai_probe_transport", fake_transport_factory)
    cli.cmd_probe(["some/model:free"], base_url="http://localhost:20128/v1", output_json=True)
    out = json.loads(capsys.readouterr().out)
    assert out[0]["ok"] is True
    assert out[0]["http_status"] == 200


def test_cmd_probe_flags_down_model(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """cmd_probe should flag a model whose transport raises."""

    def fake_transport_factory(base_url, api_key=None, opener=None):  # type: ignore[no-untyped-def]
        def transport(model_id, payload, timeout):  # type: ignore[no-untyped-def]
            raise TimeoutError("boom")

        return transport

    monkeypatch.setattr("llm_gate.probes.openai_probe_transport", fake_transport_factory)
    with pytest.raises(SystemExit):
        cli.cmd_probe(["down/model"], output_json=False)
    err_out = capsys.readouterr().out
    assert "DOWN" in err_out
