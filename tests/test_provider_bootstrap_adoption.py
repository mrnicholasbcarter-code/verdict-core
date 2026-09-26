"""Adoption coverage: every bootstrap path uses the shared contract.

The supervisor, the CLI Gate builder, the API serve path and the library
resolver must read the same precedence and report the same diagnostic classes.
Offline only: no gateway, no provider call.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from typing import Any

import pytest

import verdict.api as api

ROOT = Path(__file__).resolve().parents[1]

_VALID_CONFIG = """
primary_model: anthropic/claude-opus-5
log_path: decisions.jsonl
providers:
  omniroute:
    base_url: http://127.0.0.1:20128/v1
    api_key_env: OMNIROUTE_API_KEY
"""


def _supervisor_module():
    path = ROOT / "scripts/prime_supervisor.py"
    spec = importlib.util.spec_from_file_location("prime_supervisor_bootstrap", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str | None) -> Path:
    home = tmp_path / "home"
    config_dir = home / ".config" / "verdict"
    config_dir.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    for name in (
        "OMNIROUTE_BASE_URL",
        "OMNIROUTE_API_KEY",
        "LLMGATE_PRIMARY",
        "LLMGATE_LOG_PATH",
        "LLMGATE_INTELLIGENCE_PROFILE",
    ):
        monkeypatch.delenv(name, raising=False)
    path = config_dir / "verdict.yaml"
    if body is not None:
        path.write_text(body, encoding="utf-8")
    return path


def _write_credential_store(tmp_path: Path, values: dict[str, str]) -> Path:
    """Write a 0600 credential store under the config home used by _config_home."""
    store = tmp_path / "home" / ".config" / "verdict" / "credentials.env"
    store.write_text("".join(f"{k}={v}\n" for k, v in sorted(values.items())), encoding="utf-8")
    store.chmod(0o600)
    return store


def _spy_on_bootstrap(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Record the kwargs and the result of the next resolve_provider_bootstrap call."""
    import verdict.provider_bootstrap as pb

    seen: dict[str, Any] = {}
    real = pb.resolve_provider_bootstrap

    def spy(**kwargs: Any) -> Any:
        seen["kwargs"] = kwargs
        result = real(**kwargs)
        seen["result"] = result
        return result

    monkeypatch.setattr(pb, "resolve_provider_bootstrap", spy)
    return seen


# --- supervisor -------------------------------------------------------------


def test_supervisor_factory_reports_the_exact_missing_field_and_remediation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The opaque 'no provider configuration available' text is gone."""
    m = _supervisor_module()
    config_path = _config_home(tmp_path, monkeypatch, None)

    with pytest.raises(m.ControllerLaunchError) as excinfo:
        m._build_intelligence_service_from_config(repo=tmp_path, state_dir=tmp_path)

    exc = excinfo.value
    assert exc.reason_code == "production_factory_unavailable"
    assert "no provider configuration available for IntelligenceService" not in exc.detail
    assert "no_provider_configuration" in exc.detail
    assert "[configuration]" in exc.detail
    assert "field=providers" in exc.detail
    assert str(config_path) in exc.detail
    assert "remediation:" in exc.detail


def test_supervisor_factory_refuses_default_providers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Production never silently falls back to a built-in local provider set."""
    m = _supervisor_module()
    _config_home(tmp_path, monkeypatch, None)

    with pytest.raises(m.ControllerLaunchError) as excinfo:
        m._build_intelligence_service_from_config(repo=tmp_path, state_dir=tmp_path)

    assert "public_ollama" not in excinfo.value.detail


def test_supervisor_factory_builds_from_a_valid_config_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A valid config keeps building an IntelligenceService with the same fields."""
    m = _supervisor_module()
    _config_home(tmp_path, monkeypatch, _VALID_CONFIG)

    service = m._build_intelligence_service_from_config(repo=tmp_path, state_dir=tmp_path)

    assert service.primary_model == "anthropic/claude-opus-5"
    assert set(service.providers) == {"omniroute"}
    assert service.providers["omniroute"].base_url == "http://127.0.0.1:20128/v1"
    assert service.log_path == "decisions.jsonl"
    assert service.require_execution_path_authority is True


def test_supervisor_factory_builds_from_environment_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Environment-variable bootstrap alone is enough for the supervisor."""
    m = _supervisor_module()
    _config_home(tmp_path, monkeypatch, None)
    monkeypatch.setenv("OMNIROUTE_BASE_URL", "http://127.0.0.1:20128")

    service = m._build_intelligence_service_from_config(repo=tmp_path, state_dir=tmp_path)

    assert service.providers["omniroute"].base_url == "http://127.0.0.1:20128/v1"


def test_supervisor_factory_names_malformed_config_distinctly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Malformed config is a distinct code, not the same message as absent config."""
    m = _supervisor_module()
    _config_home(tmp_path, monkeypatch, "providers: [oops\n")

    with pytest.raises(m.ControllerLaunchError) as excinfo:
        m._build_intelligence_service_from_config(repo=tmp_path, state_dir=tmp_path)

    assert "config_file_unparsable" in excinfo.value.detail
    assert "no_provider_configuration" not in excinfo.value.detail


def test_supervisor_factory_reads_the_credential_store_as_its_own_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A key held only in the store is not credential_env_missing for the supervisor.

    The store value reports source="credential_store" and is never exported into
    os.environ.
    """
    m = _supervisor_module()
    _config_home(tmp_path, monkeypatch, _VALID_CONFIG)
    _write_credential_store(tmp_path, {"OMNIROUTE_API_KEY": "stored-token"})
    monkeypatch.delenv("OMNIROUTE_API_KEY", raising=False)
    seen = _spy_on_bootstrap(monkeypatch)

    service = m._build_intelligence_service_from_config(repo=tmp_path, state_dir=tmp_path)

    assert service.providers["omniroute"].api_key_env == "OMNIROUTE_API_KEY"
    result = seen["result"]
    codes = {note.code for note in result.notes()}
    assert "credential_env_missing" not in codes
    assert seen["kwargs"]["credential_store_env"]["OMNIROUTE_API_KEY"] == "stored-token"
    # The secret stays out of the process environment.
    assert "OMNIROUTE_API_KEY" not in os.environ


def test_supervisor_factory_still_reports_a_key_absent_from_the_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty store keeps the credential_env_missing note; nothing is invented."""
    m = _supervisor_module()
    _config_home(tmp_path, monkeypatch, _VALID_CONFIG)
    _write_credential_store(tmp_path, {"UNRELATED_TOKEN": "x"})
    monkeypatch.delenv("OMNIROUTE_API_KEY", raising=False)
    seen = _spy_on_bootstrap(monkeypatch)

    m._build_intelligence_service_from_config(repo=tmp_path, state_dir=tmp_path)

    codes = {note.code for note in seen["result"].notes()}
    assert "credential_env_missing" in codes


def test_supervisor_factory_bootstrap_resolves_a_store_supplied_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A gateway URL held only in the store bootstraps with source=credential_store."""
    m = _supervisor_module()
    _config_home(tmp_path, monkeypatch, None)
    _write_credential_store(
        tmp_path,
        {"OMNIROUTE_BASE_URL": "http://127.0.0.1:20128", "OMNIROUTE_API_KEY": "stored-token"},
    )
    seen = _spy_on_bootstrap(monkeypatch)

    service = m._build_intelligence_service_from_config(repo=tmp_path, state_dir=tmp_path)

    assert service.providers["omniroute"].base_url == "http://127.0.0.1:20128/v1"
    assert seen["result"].source_of("providers") == "credential_store"
    assert "OMNIROUTE_BASE_URL" not in os.environ


# --- CLI --------------------------------------------------------------------


def test_cli_route_gate_uses_config_file_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI Gate reads primary_model, providers and log_path from the contract."""
    from verdict.cli import _build_route_gate

    _config_home(tmp_path, monkeypatch, _VALID_CONFIG)
    gate = _build_route_gate(allow_offline=True)

    assert gate.primary_model == "anthropic/claude-opus-5"
    assert gate.providers["omniroute"].base_url == "http://127.0.0.1:20128/v1"


def test_cli_route_gate_default_providers_are_announced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Interactive CLI keeps its local default, but says so on stderr."""
    from verdict.cli import _build_route_gate

    _config_home(tmp_path, monkeypatch, None)
    gate = _build_route_gate(allow_offline=True)

    assert "public_ollama" in gate.providers
    err = capsys.readouterr().err
    assert "default_provider_fallback" in err
    assert "OMNIROUTE_BASE_URL" in err


def test_cli_route_gate_refuses_default_providers_under_production_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """profile=production must not proceed on a defaulted provider set."""
    from verdict.cli import _build_route_gate

    _config_home(tmp_path, monkeypatch, None)
    monkeypatch.setenv("LLMGATE_INTELLIGENCE_PROFILE", "production")

    with pytest.raises(SystemExit) as excinfo:
        _build_route_gate(allow_offline=True)

    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "default_providers_forbidden" in err
    assert "configuration" in err


def test_cli_route_gate_exits_with_a_named_diagnostic_on_malformed_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Malformed YAML stops the CLI with the field, source and remediation."""
    from verdict.cli import _build_route_gate

    _config_home(tmp_path, monkeypatch, "providers: 7\n")

    with pytest.raises(SystemExit):
        _build_route_gate(allow_offline=True)

    err = capsys.readouterr().err
    assert "config_providers_malformed" in err
    assert "source=config_file" in err
    assert "remediation:" in err


def test_cli_route_gate_reports_precedence_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A config/env gateway disagreement is announced, and the config still wins."""
    from verdict.cli import _build_route_gate

    _config_home(tmp_path, monkeypatch, _VALID_CONFIG)
    monkeypatch.setenv("OMNIROUTE_BASE_URL", "http://127.0.0.1:20129")

    gate = _build_route_gate(allow_offline=True)

    assert gate.providers["omniroute"].base_url == "http://127.0.0.1:20128/v1"
    assert "precedence_conflict" in capsys.readouterr().err


def test_cli_route_gate_never_echoes_url_userinfo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A password embedded in a base URL never reaches CLI stderr."""
    from verdict.cli import _build_route_gate

    _config_home(
        tmp_path,
        monkeypatch,
        "providers:\n  omniroute:\n"
        "    base_url: http://u:SECRETPW@127.0.0.1:20128/v1\n"
        "    api_key_env: OMNIROUTE_API_KEY\n",
    )
    monkeypatch.setenv("OMNIROUTE_BASE_URL", "http://envuser:ENVSECRET@127.0.0.1:20129")

    gate = _build_route_gate(allow_offline=True)

    err = capsys.readouterr().err
    assert "precedence_conflict" in err
    assert "SECRETPW" not in err
    assert "ENVSECRET" not in err
    assert "***:***@" in err
    # Execution still uses the operator's real URL.
    assert gate.providers["omniroute"].base_url == "http://u:SECRETPW@127.0.0.1:20128/v1"


def test_cli_route_gate_reports_unparsable_config_without_the_source_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The syntax-fault refusal gives a position, not the offending YAML line."""
    from verdict.cli import _build_route_gate

    _config_home(
        tmp_path,
        monkeypatch,
        "providers:\n  omniroute:\n    base_url: http://u:SECRETPW@h/v1\n  [oops\n",
    )

    with pytest.raises(SystemExit):
        _build_route_gate(allow_offline=True)

    err = capsys.readouterr().err
    assert "config_file_unparsable" in err
    assert "line 5" in err
    assert "SECRETPW" not in err


# --- library resolver -------------------------------------------------------


def test_resolve_default_providers_raises_on_malformed_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Malformed config is no longer swallowed by a bare except."""
    from verdict.gate import resolve_default_providers
    from verdict.provider_bootstrap import BootstrapError

    _config_home(tmp_path, monkeypatch, "providers: [oops\n")

    with pytest.raises(BootstrapError):
        resolve_default_providers(allow_offline=True)


def test_resolve_default_providers_announces_the_builtin_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The last-resort local set is reported on stderr."""
    from verdict.gate import resolve_default_providers

    _config_home(tmp_path, monkeypatch, None)
    _primary, providers = resolve_default_providers(allow_offline=True)

    assert set(providers) == {"omniroute", "public_ollama"}
    err = capsys.readouterr().err
    assert "default_provider_fallback" in err
    # Rendered through BootstrapDiagnostic.describe(), not a hand-built string.
    assert "[configuration] field=providers source=default:" in err
    assert "refused under profile 'production'" in err


def test_resolve_default_providers_refuses_under_the_production_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The library resolver refuses a defaulted provider set exactly like the CLI."""
    from verdict.gate import resolve_default_providers
    from verdict.provider_bootstrap import BootstrapError

    _config_home(tmp_path, monkeypatch, None)
    monkeypatch.setenv("LLMGATE_INTELLIGENCE_PROFILE", "production")

    with pytest.raises(BootstrapError) as excinfo:
        resolve_default_providers(allow_offline=True)

    diagnostic = excinfo.value.diagnostics[0]
    assert diagnostic.code == "default_providers_forbidden"
    assert diagnostic.diagnostic_class == "configuration"
    assert diagnostic.field == "providers"


def test_resolve_default_providers_refuses_when_authoritative_execution_is_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """require_authoritative refuses the built-in set in any profile."""
    from verdict.gate import resolve_default_providers
    from verdict.provider_bootstrap import BootstrapError

    _config_home(tmp_path, monkeypatch, None)

    with pytest.raises(BootstrapError) as excinfo:
        resolve_default_providers(allow_offline=True, require_authoritative=True)

    assert excinfo.value.diagnostics[0].code == "default_providers_forbidden"


def test_gate_refuses_default_providers_under_production_without_port_scanning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Gate() path (MCP server, comparison harness) fails closed under production.

    The refusal is taken from the offline contract, so no provider detection
    (a live localhost port scan) runs first.
    """
    import verdict.provider_detection as provider_detection
    from verdict.gate import Gate
    from verdict.provider_bootstrap import BootstrapError

    _config_home(tmp_path, monkeypatch, None)
    monkeypatch.setenv("LLMGATE_INTELLIGENCE_PROFILE", "production")

    calls: list[object] = []

    def _spy(*args: object, **kwargs: object) -> dict[str, object]:
        calls.append(args)
        return {}

    monkeypatch.setattr(provider_detection, "detect_all_providers", _spy)

    with pytest.raises(BootstrapError) as excinfo:
        Gate()

    assert excinfo.value.diagnostics[0].code == "default_providers_forbidden"
    assert calls == []


def test_gate_still_builds_the_builtin_set_outside_production(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Interactive local use keeps the built-in set when detection finds nothing."""
    import verdict.provider_detection as provider_detection
    from verdict.gate import Gate

    _config_home(tmp_path, monkeypatch, None)
    monkeypatch.setattr(provider_detection, "detect_all_providers", lambda *a, **k: {})
    monkeypatch.setattr(
        provider_detection, "generate_verdict_config", lambda *a, **k: {"providers": {}}
    )

    gate = Gate()

    assert set(gate.providers) == {"omniroute", "public_ollama"}


# --- API serve path ---------------------------------------------------------


def test_server_bootstrap_diagnostics_separate_configuration_from_gateway_health(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Startup diagnostics name a configuration failure without probing a gateway."""
    _config_home(tmp_path, monkeypatch, None)
    monkeypatch.delenv("LLMGATE_UPSTREAM_BASE_URL", raising=False)

    report = api.server_bootstrap_diagnostics()

    assert report["status"] == "configuration_incomplete"
    assert report["gateway_required"] is False
    codes = {entry["code"] for entry in report["diagnostics"]}
    assert "no_provider_configuration" in codes
    assert "default_upstream_base_url" in codes
    assert {entry["class"] for entry in report["diagnostics"]} == {"configuration"}


def test_server_bootstrap_diagnostics_report_gateway_required_for_lifecycle_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gateway_required and gateway_url are exposed; no gateway is started."""
    _config_home(tmp_path, monkeypatch, _VALID_CONFIG)
    monkeypatch.delenv("LLMGATE_UPSTREAM_BASE_URL", raising=False)

    report = api.server_bootstrap_diagnostics()

    assert report["status"] == "ok"
    assert report["gateway_required"] is True
    assert report["gateway_url"] == "http://127.0.0.1:20128/v1"
    assert report["field_sources"]["providers"] == "config_file"


def test_serve_intelligence_resolves_identity_from_environment_then_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Serve precedence for primary_model/profile/log_path is env, then default.

    Rewritten from an earlier assertion that the serve path takes these three
    fields from verdict.yaml. Reading them from the routing config would silently
    change a running server's identity, profile and decision log the moment a
    config file appeared, so the serve path keeps environment-then-default. The
    config value is not ignored in silence: it is reported as a
    precedence_conflict. This asserts every field the old test did, plus the
    source, the conflict code and the remediation.
    """
    from verdict.contracts import DEFAULT_PRIMARY_MODEL
    from verdict.intelligence import DEFAULT_PROFILE

    body = _VALID_CONFIG.replace("anthropic/claude-opus-5", "vendor/cfg-model")
    _config_home(tmp_path, monkeypatch, body + "profile: staging\n")

    service = api._build_intelligence()

    assert service.primary_model == DEFAULT_PRIMARY_MODEL
    assert service.profile == DEFAULT_PROFILE
    assert service.log_path == "verdict-decisions.jsonl"
    assert service.providers == {}

    err = capsys.readouterr().err
    assert err.count("precedence_conflict") == 3
    for field_name, env_name in (
        ("primary_model", "LLMGATE_PRIMARY"),
        ("profile", "LLMGATE_INTELLIGENCE_PROFILE"),
        ("log_path", "LLMGATE_LOG_PATH"),
    ):
        assert f"field={field_name}" in err
        assert env_name in err
    assert "vendor/cfg-model" in err
    assert "staging" in err
    assert "decisions.jsonl" in err


def test_serve_intelligence_applies_the_environment_over_the_config_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """On an env/config disagreement the environment wins and the conflict is named."""
    _config_home(tmp_path, monkeypatch, _VALID_CONFIG)
    monkeypatch.setenv("LLMGATE_PRIMARY", "vendor/env-model")

    service = api._build_intelligence()

    assert service.primary_model == "vendor/env-model"
    err = capsys.readouterr().err
    assert "precedence_conflict" in err
    assert "field=primary_model" in err
    assert "source=environment" in err
    assert "anthropic/claude-opus-5" in err
    assert "vendor/env-model" in err


def test_serve_intelligence_does_not_let_the_config_file_flip_the_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A YAML 'profile: production' must not change serve's profile.

    The serve profile governs execution-path authority and /v1/route/explain
    exclusions. A config file appearing on disk must not flip it.
    """
    from verdict.intelligence import DEFAULT_PROFILE

    _config_home(tmp_path, monkeypatch, _VALID_CONFIG + "profile: production\n")

    service = api._build_intelligence()

    assert service.profile == DEFAULT_PROFILE
    assert service.profile != "production"
    err = capsys.readouterr().err
    assert "precedence_conflict" in err
    assert "field=profile" in err
    assert "LLMGATE_INTELLIGENCE_PROFILE=production" in err


def test_serve_intelligence_reports_no_conflict_when_sources_agree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Matching env and config values are not a conflict."""
    _config_home(tmp_path, monkeypatch, _VALID_CONFIG)
    monkeypatch.setenv("LLMGATE_PRIMARY", "anthropic/claude-opus-5")
    monkeypatch.setenv("LLMGATE_LOG_PATH", "decisions.jsonl")

    service = api._build_intelligence()

    assert service.primary_model == "anthropic/claude-opus-5"
    assert service.log_path == "decisions.jsonl"
    assert "precedence_conflict" not in capsys.readouterr().err


def test_serve_intelligence_survives_incomplete_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Serve still boots without config; admission happens per request."""
    from verdict import api
    from verdict.contracts import DEFAULT_PRIMARY_MODEL
    from verdict.intelligence import DEFAULT_PROFILE

    _config_home(tmp_path, monkeypatch, None)

    service = api._build_intelligence()

    assert service.primary_model == DEFAULT_PRIMARY_MODEL
    assert service.profile == DEFAULT_PROFILE


def test_upstream_base_url_source_names_the_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The built-in upstream default is labelled 'default', never silent."""
    _config_home(tmp_path, monkeypatch, None)
    monkeypatch.delenv("LLMGATE_UPSTREAM_BASE_URL", raising=False)
    assert api._upstream_base_url_source() == "default"

    monkeypatch.setenv("OMNIROUTE_BASE_URL", "http://127.0.0.1:20128")
    assert api._upstream_base_url_source() == "environment"


def test_server_bootstrap_diagnostics_redact_url_userinfo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The serve report dict never carries a URL password, even though it is not rendered today."""
    monkeypatch.setenv("OMNIROUTE_BASE_URL", "http://u:SECRETPW@127.0.0.1:20128")
    monkeypatch.setenv("LLMGATE_UPSTREAM_BASE_URL", "http://u:SECRETPW@127.0.0.1:20128/v1")
    _config_home(tmp_path, monkeypatch, _VALID_CONFIG)

    report = api.server_bootstrap_diagnostics()

    assert "SECRETPW" not in repr(report)
