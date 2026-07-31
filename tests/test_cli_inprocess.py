"""In-process tests for CLI command helpers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from verdict import cli
from verdict.provider_detection import DetectedProvider, DetectionResult


@pytest.fixture(autouse=True)
def isolated_config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))


def test_cmd_route_terse_uses_configured_primary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
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

    cli.cmd_route("deploy prod", "critical", terse=True)

    assert capsys.readouterr().out.strip() == "test-primary"


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

    monkeypatch.setattr(cli.sys, "argv", ["verdict", "route", "hello", "--terse"])
    cli.main()
    assert "anthropic/claude-3-opus-20240229" in capsys.readouterr().out

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


def test_cmd_doctor_all_healthy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))

    cfg_dir = tmp_path / ".config" / "verdict"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "verdict.yaml").write_text(
        "primary_model: anthropic/claude-3-opus-20240229\n"
        "log_path: route-log.jsonl\n"
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

    import verdict.documentation_preflight as documentation_preflight

    monkeypatch.setattr(documentation_preflight, "discover_sources", lambda _root=None: ())

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


def test_cli_doctor_json_has_machine_readable_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import sys

    import verdict.documentation_preflight as documentation_preflight

    monkeypatch.setattr(documentation_preflight, "discover_sources", lambda _root=None: ())
    monkeypatch.setattr(cli, "_omniroute_api_request", lambda *args, **kwargs: None)
    monkeypatch.setattr(sys, "argv", ["verdict", "doctor", "--json"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    report = json.loads(capsys.readouterr().out)
    assert report["documentation_preflight"]["status"] == "ready"
    assert report["status"] == "issues_found"


def test_cmd_doctor_issues_and_duplicates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))

    # Config with issue: literal API key in URL, and duplicate base_url
    cfg_dir = tmp_path / ".config" / "verdict"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "verdict.yaml").write_text(
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

    cli.cmd_doctor()

    out = capsys.readouterr().out
    assert "Literal API key detected inside the host URL for provider" in out
    assert "Duplicate host URL configured in verdict.yaml" in out
    assert "Duplicate node 'Ollama2'" in out
    assert "node2" in deleted_nodes


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

    cfg_dir = tmp_path / ".config" / "verdict"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "verdict.yaml").write_text(
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
