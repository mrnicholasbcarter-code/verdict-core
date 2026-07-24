"""Provider detection tests with deterministic mocked environment."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from verdict import provider_detection as pd
from verdict.provider_detection import DetectedProvider, DetectionResult


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))


def test_detection_result_flattening_and_presence() -> None:
    result = DetectionResult(
        local_servers=[DetectedProvider(id="ollama", name="Ollama", type="local_server")],
        cloud_apis=[DetectedProvider(id="bedrock", name="Bedrock", type="cloud_api")],
    )

    assert [p.id for p in result.all_providers()] == ["ollama", "bedrock"]
    assert result.has_any_provider() is True
    assert DetectionResult().has_any_provider() is False


def test_detect_local_servers_installed_and_running(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        pd,
        "LOCAL_SERVERS",
        {
            "ollama": {
                "cli_name": "ollama",
                "default_base_url": "http://localhost:11434/v1",
                "models_endpoint": "/api/tags",
                "detect_running": lambda: True,
            },
            "lmstudio": {
                "cli_name": "lms",
                "default_base_url": "http://localhost:1234/v1",
                "models_endpoint": "/models",
                "detect_running": lambda: False,
            },
        },
    )
    monkeypatch.setattr(pd, "_which", lambda cmd: f"/bin/{cmd}" if cmd == "ollama" else None)
    monkeypatch.setattr(pd, "_fetch_models_from_server", lambda *_args: ["llama3.1", "qwen2.5"])

    detected = pd.detect_local_servers()

    assert [p.id for p in detected] == ["ollama"]
    assert detected[0].server_running is True
    assert detected[0].cli_available is True
    assert detected[0].models == ["llama3.1", "qwen2.5"]


def test_fetch_models_from_openai_and_server_specific_formats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeResponse:
        def __init__(self, status_code: int, payload: object) -> None:
            self.status_code = status_code
            self._payload = payload

        def json(self) -> object:
            return self._payload

    class FakeClient:
        def __init__(self, timeout: float) -> None:
            self.timeout = timeout
            self.calls: list[str] = []

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def get(self, url: str) -> FakeResponse:
            self.calls.append(url)
            if url.endswith("/models"):
                return FakeResponse(200, {"data": [{"id": "gpt-a"}, {"id": "gpt-b"}]})
            raise AssertionError(url)

    monkeypatch.setattr(pd.httpx, "Client", FakeClient)

    assert pd._fetch_models_from_server("http://localhost:1234/v1", "/api/tags") == [
        "gpt-a",
        "gpt-b",
    ]


def test_fetch_models_from_fallback_endpoint_and_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeResponse:
        def __init__(self, status_code: int, payload: object) -> None:
            self.status_code = status_code
            self._payload = payload

        def json(self) -> object:
            return self._payload

    class FakeClient:
        def __init__(self, timeout: float) -> None:
            self.timeout = timeout

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def get(self, url: str) -> FakeResponse:
            if url.endswith("/models"):
                return FakeResponse(404, {})
            if url.endswith("/api/tags"):
                return FakeResponse(200, {"models": [{"name": "local-a"}, {"id": "local-b"}]})
            raise AssertionError(url)

    monkeypatch.setattr(pd.httpx, "Client", FakeClient)
    assert pd._fetch_models_from_server("http://localhost:11434/v1", "/api/tags") == [
        "local-a",
        "local-b",
    ]

    class RaisingClient(FakeClient):
        def get(self, url: str) -> FakeResponse:
            raise OSError("network down")

    monkeypatch.setattr(pd.httpx, "Client", RaisingClient)
    assert pd._fetch_models_from_server("http://localhost:11434/v1", "/api/tags") == []


def test_detect_cli_providers_with_env_and_config_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / ".config" / "anthropic"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(json.dumps({"access_token": "token"}))

    monkeypatch.setattr(pd, "PROVIDER_CLIS", {"anthropic": ["claude"], "openai": ["openai"]})
    monkeypatch.setattr(
        pd, "API_KEY_ENV_VARS", {"anthropic": ["ANTHROPIC_API_KEY"], "openai": ["OPENAI_API_KEY"]}
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(pd, "_which", lambda cmd: f"/usr/bin/{cmd}")

    detected = {p.id: p for p in pd.detect_cli_providers()}

    assert detected["anthropic"].api_key_configured is True
    assert detected["openai"].api_key_configured is True
    assert detected["openai"].api_key_env == "OPENAI_API_KEY"


def test_provider_auth_ignores_invalid_json(tmp_path: Path) -> None:
    config_dir = tmp_path / ".config" / "badprovider"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text("not json")

    assert pd._check_provider_auth("badprovider") is False


def test_detect_centralized_routers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        pd,
        "CENTRALIZED_ROUTERS",
        {
            "9router": {
                "default_base_url": "http://localhost:20128/v1",
                "models_endpoint": "/models",
                "detect_running": lambda: True,
                "install_hint": "npm install -g 9router",
                "github": "https://github.com/1jehuang/9router",
                "description": "Router",
            }
        },
    )
    monkeypatch.setattr(pd, "_which", lambda _cmd: None)
    monkeypatch.setattr(pd, "_fetch_models_from_server", lambda *_args: ["router-model"])

    detected = pd.detect_centralized_routers()

    assert detected[0].id == "9router"
    assert detected[0].server_running is True
    assert detected[0].models == ["router-model"]


def test_detect_cloud_apis_skips_cli_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pd, "PROVIDER_CLIS", {"openai": ["openai"]})
    monkeypatch.setattr(
        pd, "API_KEY_ENV_VARS", {"openai": ["OPENAI_API_KEY"], "bedrock": ["AWS_ACCESS_KEY_ID"]}
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "aws-test")

    detected = pd.detect_cloud_apis()

    assert [p.id for p in detected] == ["bedrock"]
    assert detected[0].description == "API key found in AWS_ACCESS_KEY_ID"


def test_detect_custom_endpoints_from_env_and_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = tmp_path / ".config" / "verdict"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "verdict.yaml").write_text(
        "providers:\n"
        "  local-router:\n"
        "    base_url: http://localhost:20128/v1\n"
        "    api_key_env: ROUTER_KEY\n"
    )
    monkeypatch.setenv("OPENAI_BASE_URL", "http://localhost:9999/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-custom")
    monkeypatch.setenv("ROUTER_KEY", "router-secret")
    monkeypatch.setattr(pd, "_check_port_from_url", lambda url: url.endswith("9999/v1"))
    monkeypatch.setattr(pd, "_fetch_models_from_server", lambda *_args: ["custom-model"])

    detected = pd.detect_custom_endpoints()

    assert [p.id for p in detected] == ["custom", "custom:local-router"]
    assert detected[0].models == ["custom-model"]
    assert detected[0].server_running is True
    assert detected[1].api_key_configured is True


def test_check_port_from_url_handles_default_ports_and_bad_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[int, str]] = []

    def fake_check_port(port: int, host: str = "127.0.0.1") -> bool:
        seen.append((port, host))
        return True

    monkeypatch.setattr(pd, "_check_port", fake_check_port)

    assert pd._check_port_from_url("https://example.com/path") is True
    assert seen == [(443, "example.com")]

    monkeypatch.setattr(pd, "_check_port", lambda *_args: (_ for _ in ()).throw(ValueError()))
    assert pd._check_port_from_url("not a url") is False


def test_detect_all_providers_aggregates_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        pd,
        "detect_local_servers",
        lambda: [DetectedProvider(id="local", name="Local", type="local_server")],
    )
    monkeypatch.setattr(
        pd,
        "detect_cli_providers",
        lambda: [DetectedProvider(id="cli", name="CLI", type="cli_provider")],
    )
    monkeypatch.setattr(pd, "detect_centralized_routers", lambda: [])
    monkeypatch.setattr(pd, "detect_cloud_apis", lambda: [])
    monkeypatch.setattr(pd, "detect_custom_endpoints", lambda: [])

    result = pd.detect_all_providers()

    assert [p.id for p in result.all_providers()] == ["local", "cli"]


def test_format_detection_report_empty_and_verbose_recommendations() -> None:
    empty_report = pd.format_detection_report(DetectionResult())
    assert "No providers detected" in empty_report

    result = DetectionResult(
        centralized_routers=[
            DetectedProvider(
                id="9router",
                name="9router",
                type="centralized_router",
                base_url="http://localhost:20128/v1",
                models=["model-a", "model-b"],
                server_running=True,
                install_hint="npm install -g 9router",
            )
        ],
        cli_providers=[
            DetectedProvider(
                id="openai",
                name="OpenAI",
                type="cli_provider",
                api_key_env="OPENAI_API_KEY",
                api_key_configured=False,
                cli_available=True,
            )
        ],
    )

    report = pd.format_detection_report(result, verbose=True)

    assert "Centralized router detected" in report
    assert "model-a, model-b" in report
    assert "Needs OPENAI_API_KEY" in report


def test_format_detection_report_recommends_router_when_only_cloud_or_local() -> None:
    result = DetectionResult(
        cloud_apis=[
            DetectedProvider(
                id="bedrock", name="Bedrock", type="cloud_api", api_key_configured=True
            )
        ]
    )

    report = pd.format_detection_report(result)

    assert "Install a centralized router" in report


def test_generate_config_prefers_router_local_then_cloud() -> None:
    router_config = pd.generate_verdict_config(
        DetectionResult(
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
    )
    assert router_config["primary_model"] == "router-primary"
    assert router_config["providers"]["9router"]["base_url"] == "http://localhost:20128/v1"

    local_config = pd.generate_verdict_config(
        DetectionResult(
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
    )
    assert local_config["primary_model"] == "llama3"

    anthropic_config = pd.generate_verdict_config(
        DetectionResult(
            cli_providers=[
                DetectedProvider(
                    id="anthropic", name="Anthropic", type="cli_provider", api_key_configured=True
                )
            ]
        )
    )
    assert anthropic_config["providers"]["anthropic"]["api_key_env"] == "ANTHROPIC_API_KEY"

    openrouter_config = pd.generate_verdict_config(
        DetectionResult(
            cli_providers=[
                DetectedProvider(
                    id="openai", name="OpenAI", type="cli_provider", api_key_configured=True
                )
            ]
        )
    )
    assert openrouter_config["providers"]["openrouter"]["api_key_env"] == "OPENROUTER_API_KEY"

    empty_config = pd.generate_verdict_config(DetectionResult())
    assert empty_config == {"primary_model": "anthropic/claude-3-opus-20240229", "providers": {}}
