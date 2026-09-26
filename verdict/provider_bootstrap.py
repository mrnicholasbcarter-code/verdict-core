"""Deterministic provider/gateway bootstrap contract.

One offline resolver shared by the CLI, the API server, the Prime supervisor and
tests. It answers exactly one question: which provider and gateway configuration
did the operator actually supply, and if that configuration is incomplete, which
field from which source is missing and how is it fixed.

Boundaries:

- Pure and offline. Reading a config file is the only I/O; no network call, no
  port scan, no subprocess, no gateway start or stop.
- Never invents a provider. An absent provider map is a named refusal, not a
  quiet substitution of a local server or of the controller model.
- Candidate admission, eligibility and ranking stay outside this module. The
  ``model_eligibility`` diagnostic class exists so startup diagnostics can name
  that failure class distinctly; this module never decides eligibility.
- ``gateway_required`` and ``gateway_url`` are reported, never acted on. Gateway
  lifecycle management belongs to its own component.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from verdict.contracts import DEFAULT_PRIMARY_MODEL
from verdict.models import ProviderConfig

__all__ = [
    "DEFAULT_LOCAL_PROVIDERS",
    "PRODUCTION_PROFILE",
    "BootstrapDiagnostic",
    "BootstrapError",
    "BootstrapResult",
    "BootstrapSource",
    "DiagnosticClass",
    "GatewayProbeResult",
    "ProviderBinding",
    "bootstrap_config_path",
    "describe_bootstrap_failure",
    "load_credential_store_env",
    "redact_url",
    "resolve_provider_bootstrap",
    "verify_gateway_reachable",
]

#: Canonical configuration sources, in the order the contract documents them.
BootstrapSource = Literal["environment", "config_file", "credential_store", "default"]

#: Startup failure classes. Keeping these disjoint is the point of the contract:
#: a missing field is never reported as an unhealthy gateway, and an unhealthy
#: gateway is never reported as an ineligible model.
DiagnosticClass = Literal["configuration", "gateway_health", "model_eligibility"]

#: Default intelligence profile. Mirrors ``verdict.intelligence.DEFAULT_PROFILE``
#: without importing that module (bootstrap must stay import-cheap).
DEFAULT_BOOTSTRAP_PROFILE = "development"
DEFAULT_BOOTSTRAP_LOG_PATH = "verdict-decisions.jsonl"

#: Profile under which a defaulted provider set is refused outright.
PRODUCTION_PROFILE = "production"

#: Built-in local provider set. Offered only for interactive local use, only
#: when the caller opts in, and always with a visible ``default_provider_fallback``
#: diagnostic naming the default and how to configure it explicitly.
DEFAULT_LOCAL_PROVIDERS: Mapping[str, str] = {
    "omniroute": "http://localhost:20128/v1",
    "public_ollama": "http://localhost:11434/v1",
}

_GATEWAY_ENV = "OMNIROUTE_BASE_URL"
_GATEWAY_KEY_ENV = "OMNIROUTE_API_KEY"
_GATEWAY_PROVIDER_NAME = "omniroute"
_PRIMARY_MODEL_ENV = "LLMGATE_PRIMARY"
_PROFILE_ENV = "LLMGATE_INTELLIGENCE_PROFILE"
_LOG_PATH_ENV = "LLMGATE_LOG_PATH"

_REMEDIATION_PROVIDERS = (
    "set OMNIROUTE_BASE_URL to the gateway base URL, or add a 'providers' "
    "mapping to {config_path} (run 'verdict setup' or 'verdict detect')"
)


@dataclass(frozen=True)
class BootstrapDiagnostic:
    """One named, actionable bootstrap finding.

    ``field`` is the configuration field at fault, ``source`` the source that
    supplied (or failed to supply) it, and ``remediation`` the operator action.
    """

    code: str
    diagnostic_class: DiagnosticClass
    field: str
    detail: str
    remediation: str
    source: BootstrapSource | None = None
    fatal: bool = True

    def describe(self) -> str:
        """Render a deterministic single-line description."""
        origin = f" source={self.source}" if self.source is not None else ""
        return (
            f"{self.code} [{self.diagnostic_class}] field={self.field}{origin}: "
            f"{self.detail}; remediation: {self.remediation}"
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a stable, sorted-key-friendly mapping."""
        return {
            "code": self.code,
            "class": self.diagnostic_class,
            "field": self.field,
            "source": self.source,
            "detail": self.detail,
            "remediation": self.remediation,
            "fatal": self.fatal,
        }


class BootstrapError(Exception):
    """Fail-closed bootstrap refusal carrying every fatal diagnostic."""

    def __init__(self, reason_code: str, diagnostics: tuple[BootstrapDiagnostic, ...]) -> None:
        if not diagnostics:
            raise ValueError("BootstrapError requires at least one diagnostic")
        self.reason_code = reason_code
        self.diagnostics = diagnostics
        super().__init__(f"{reason_code}: " + " | ".join(d.describe() for d in diagnostics))

    @property
    def diagnostic_class(self) -> DiagnosticClass:
        """Class of the first fatal diagnostic."""
        return self.diagnostics[0].diagnostic_class

    def to_dict(self) -> dict[str, Any]:
        """Serialize the refusal for receipts and structured logs."""
        return {
            "reason_code": self.reason_code,
            "class": self.diagnostic_class,
            "diagnostics": [d.to_dict() for d in self.diagnostics],
        }


@dataclass(frozen=True)
class ProviderBinding:
    """One resolved provider, with the source that supplied it."""

    name: str
    base_url: str
    source: BootstrapSource
    api_key_env: str | None = None

    def to_provider_config(self) -> ProviderConfig:
        """Convert to the runtime ``ProviderConfig``."""
        return ProviderConfig(base_url=self.base_url, api_key_env=self.api_key_env)

    def to_dict(self) -> dict[str, Any]:
        """Serialize without secret material (env names only, URL userinfo redacted)."""
        return {
            "name": self.name,
            "base_url": redact_url(self.base_url),
            "api_key_env": self.api_key_env,
            "source": self.source,
        }


@dataclass(frozen=True)
class GatewayProbeResult:
    """Outcome of an externally supplied gateway health probe."""

    reachable: bool
    detail: str = ""


@dataclass(frozen=True)
class BootstrapResult:
    """Validated provider/gateway configuration plus its provenance."""

    primary_model: str
    providers: Mapping[str, ProviderBinding]
    gateway_required: bool
    gateway_url: str | None
    profile: str
    log_path: str
    config_path: Path
    config_present: bool
    field_sources: Mapping[str, BootstrapSource]
    diagnostics: tuple[BootstrapDiagnostic, ...] = field(default_factory=tuple)

    def provider_configs(self) -> dict[str, ProviderConfig]:
        """Runtime provider map for ``Gate`` / ``IntelligenceService``."""
        return {name: binding.to_provider_config() for name, binding in self.providers.items()}

    def source_of(self, field_name: str) -> BootstrapSource | None:
        """Source that won precedence for ``field_name``."""
        return self.field_sources.get(field_name)

    def notes(self) -> tuple[BootstrapDiagnostic, ...]:
        """Non-fatal diagnostics (precedence conflicts, absent optional input)."""
        return tuple(d for d in self.diagnostics if not d.fatal)

    def to_dict(self) -> dict[str, Any]:
        """Serialize the whole contract for diagnostics output."""
        return {
            "primary_model": self.primary_model,
            "providers": {name: b.to_dict() for name, b in sorted(self.providers.items())},
            "gateway_required": self.gateway_required,
            "gateway_url": None if self.gateway_url is None else redact_url(self.gateway_url),
            "profile": self.profile,
            "log_path": self.log_path,
            "config_path": str(self.config_path),
            "config_present": self.config_present,
            "field_sources": dict(sorted(self.field_sources.items())),
            "diagnostics": [d.to_dict() for d in self.diagnostics],
        }


def bootstrap_config_path(
    env: Mapping[str, str] | None = None, *, home: Path | None = None
) -> Path:
    """Canonical routing-YAML path: ``$XDG_CONFIG_HOME/verdict/verdict.yaml``."""
    source = os.environ if env is None else env
    xdg = (source.get("XDG_CONFIG_HOME") or "").strip()
    base = Path(xdg) if xdg else (home or Path.home()) / ".config"
    return base / "verdict" / "verdict.yaml"


#: Matches the ``user:password@`` userinfo of any ``scheme://`` URL, including a
#: URL embedded in a longer message such as an exception string.
_USERINFO_RE = re.compile(r"(?<=://)[^/?#\s]*@")


def load_credential_store_env() -> dict[str, str]:
    """Read the local credential store for the ``credential_store_env`` seam.

    Returns the stored ``name -> value`` mapping, or ``{}`` when no store exists
    or it refuses to load (insecure permissions, unreadable file). Values are
    handed to ``resolve_provider_bootstrap`` as an argument and are never exported
    into ``os.environ``: a stored credential must not leak into child processes
    or into unrelated code that reads the environment.

    A name supplied this way reports ``source="credential_store"`` and does not
    raise ``credential_env_missing``.
    """
    try:
        from verdict.credentials_store import CredentialsStore

        return {str(k): str(v) for k, v in CredentialsStore().load().items()}
    except Exception:
        # A missing, unreadable or insecure store is not a bootstrap failure; the
        # resolver still reports any name that stays unset as credential_env_missing.
        return {}


def redact_url(value: str) -> str:
    """Replace any ``user:password@`` userinfo with ``***:***@``.

    Operators sometimes embed credentials in a base URL. Every place that echoes
    a URL - diagnostics, serialized bindings, stderr notes - passes through here,
    so a password never reaches a log, a receipt or a terminal. The pattern also
    redacts a URL embedded in a longer string, so wrapped error text is safe too.
    """
    return _USERINFO_RE.sub("***:***@", str(value))


def _text(source: Mapping[str, str], name: str) -> str:
    return (source.get(name) or "").strip()


def _normalize_gateway_url(raw: str) -> str:
    """Strip trailing slashes; keep the operator's path as given."""
    return raw.strip().rstrip("/")


def _openai_base(raw: str) -> str:
    """Append ``/v1`` unless the operator already supplied it."""
    url = _normalize_gateway_url(raw)
    return url if url.endswith("/v1") else f"{url}/v1"


def _validate_url(
    value: str, *, field_name: str, source: BootstrapSource
) -> BootstrapDiagnostic | None:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return BootstrapDiagnostic(
            code="provider_base_url_invalid",
            diagnostic_class="configuration",
            field=field_name,
            source=source,
            detail=f"{redact_url(value)!r} is not an absolute http(s) URL",
            remediation=f"set {field_name} to an absolute URL such as http://127.0.0.1:20128",
        )
    return None


def _yaml_error_position(exc: Exception) -> str:
    """Locate a YAML syntax fault without echoing the offending source line.

    A malformed routing config can hold a credential on the very line the parser
    rejected, and PyYAML puts that line into ``str(exc)``. Only the parser's
    problem text plus the 1-based line and column are reported.
    """
    mark = getattr(exc, "problem_mark", None)
    problem = str(getattr(exc, "problem", "") or "").strip()
    if mark is None:
        return f"({type(exc).__name__})" if not problem else f"({problem})"
    where = f"at line {int(mark.line) + 1} column {int(mark.column) + 1}"
    return f"({problem} {where})" if problem else f"({where})"


def _load_config_file(config_path: Path) -> tuple[dict[str, Any], bool, BootstrapDiagnostic | None]:
    """Read the routing YAML. Malformed content is fatal, absence is not."""
    if not config_path.is_file():
        return (
            {},
            False,
            BootstrapDiagnostic(
                code="config_file_missing",
                diagnostic_class="configuration",
                field="config_file",
                source="config_file",
                detail=f"no routing config at {config_path}",
                remediation=(
                    f"run 'verdict setup' to write {config_path}, or supply "
                    f"{_GATEWAY_ENV} in the environment"
                ),
                fatal=False,
            ),
        )
    try:
        import yaml

        raw = yaml.safe_load(config_path.read_text("utf-8"))
    except OSError as exc:
        raise BootstrapError(
            "bootstrap_configuration_invalid",
            (
                BootstrapDiagnostic(
                    code="config_file_unreadable",
                    diagnostic_class="configuration",
                    field="config_file",
                    source="config_file",
                    detail=f"cannot read {config_path}: {exc}",
                    remediation=f"make {config_path} readable by this user, or remove it",
                ),
            ),
        ) from exc
    except Exception as exc:
        raise BootstrapError(
            "bootstrap_configuration_invalid",
            (
                BootstrapDiagnostic(
                    code="config_file_unparsable",
                    diagnostic_class="configuration",
                    field="config_file",
                    source="config_file",
                    detail=f"{config_path} is not valid YAML {_yaml_error_position(exc)}",
                    remediation=f"fix the YAML syntax in {config_path}, or rerun 'verdict setup'",
                ),
            ),
        ) from exc
    if raw is None:
        return {}, True, None
    if not isinstance(raw, dict):
        raise BootstrapError(
            "bootstrap_configuration_invalid",
            (
                BootstrapDiagnostic(
                    code="config_file_not_mapping",
                    diagnostic_class="configuration",
                    field="config_file",
                    source="config_file",
                    detail=f"{config_path} must contain a YAML mapping, found {type(raw).__name__}",
                    remediation=f"replace {config_path} with a mapping, or rerun 'verdict setup'",
                ),
            ),
        )
    return dict(raw), True, None


def _providers_from_config(
    raw: Mapping[str, Any], *, config_path: Path
) -> dict[str, ProviderBinding]:
    section = raw.get("providers")
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise BootstrapError(
            "bootstrap_configuration_invalid",
            (
                BootstrapDiagnostic(
                    code="config_providers_malformed",
                    diagnostic_class="configuration",
                    field="providers",
                    source="config_file",
                    detail=(
                        f"'providers' in {config_path} must be a mapping, "
                        f"found {type(section).__name__}"
                    ),
                    remediation=f"rewrite the 'providers' mapping in {config_path}",
                ),
            ),
        )
    fatal: list[BootstrapDiagnostic] = []
    bindings: dict[str, ProviderBinding] = {}
    for name, value in section.items():
        label = f"providers.{name}"
        if not isinstance(value, dict):
            fatal.append(
                BootstrapDiagnostic(
                    code="provider_entry_malformed",
                    diagnostic_class="configuration",
                    field=label,
                    source="config_file",
                    detail=f"{label} must be a mapping, found {type(value).__name__}",
                    remediation=f"give {label} a 'base_url' mapping in {config_path}",
                )
            )
            continue
        base_url = str(value.get("base_url") or "").strip()
        if not base_url:
            fatal.append(
                BootstrapDiagnostic(
                    code="provider_base_url_missing",
                    diagnostic_class="configuration",
                    field=f"{label}.base_url",
                    source="config_file",
                    detail=f"{label} has no 'base_url'",
                    remediation=f"set {label}.base_url in {config_path}",
                )
            )
            continue
        invalid = _validate_url(base_url, field_name=f"{label}.base_url", source="config_file")
        if invalid is not None:
            fatal.append(invalid)
            continue
        api_key_env = value.get("api_key_env")
        bindings[str(name)] = ProviderBinding(
            name=str(name),
            base_url=base_url,
            source="config_file",
            api_key_env=str(api_key_env) if api_key_env else None,
        )
    if fatal:
        raise BootstrapError("bootstrap_configuration_invalid", tuple(fatal))
    return bindings


def _looks_like_gateway(binding: ProviderBinding) -> bool:
    """True when a binding points at an OmniRoute-style gateway."""
    return binding.name == _GATEWAY_PROVIDER_NAME or binding.api_key_env == _GATEWAY_KEY_ENV


def resolve_provider_bootstrap(
    *,
    env: Mapping[str, str] | None = None,
    config_path: Path | None = None,
    home: Path | None = None,
    credential_store_env: Mapping[str, str] | None = None,
    allow_default_providers: bool = False,
    default_providers: Mapping[str, str] | None = None,
    require_authoritative: bool = False,
) -> BootstrapResult:
    """Resolve provider/gateway configuration from the canonical sources.

    Precedence (documented in ``docs/CONFIGURATION.md``):

    * ``providers``: config file, then ``OMNIROUTE_BASE_URL``. No default.
    * ``gateway_url``: ``OMNIROUTE_BASE_URL``, then config ``gateway_url``.
    * ``primary_model`` / ``profile`` / ``log_path``: config file, then
      environment, then built-in default.
    * any environment name: exported environment, then credential store.

    Raises ``BootstrapError`` when configuration is incomplete or malformed.
    Never probes the network.

    A provider set is normally required from a canonical source. Interactive
    local callers may pass ``allow_default_providers=True`` to accept the
    built-in local set; that path is never silent — ``field_sources["providers"]``
    becomes ``"default"`` and a ``default_provider_fallback`` diagnostic names the
    default and how to configure it. Under profile ``production``, or when the
    caller passes ``require_authoritative=True``, a defaulted provider set is a
    ``configuration`` ``BootstrapError`` and execution must not proceed.
    """
    exported = dict(os.environ if env is None else env)
    merged: dict[str, str] = dict(credential_store_env or {})
    store_supplied = {key for key in merged if not (exported.get(key) or "").strip()}
    merged.update({k: v for k, v in exported.items() if (v or "").strip()})

    def env_source(name: str) -> BootstrapSource:
        return "credential_store" if name in store_supplied else "environment"

    path = config_path or bootstrap_config_path(exported, home=home)
    raw, present, missing_note = _load_config_file(path)

    diagnostics: list[BootstrapDiagnostic] = []
    if missing_note is not None:
        diagnostics.append(missing_note)

    field_sources: dict[str, BootstrapSource] = {}

    primary_model = DEFAULT_PRIMARY_MODEL
    field_sources["primary_model"] = "default"
    env_primary = _text(merged, _PRIMARY_MODEL_ENV)
    if env_primary:
        primary_model = env_primary
        field_sources["primary_model"] = env_source(_PRIMARY_MODEL_ENV)
    if raw.get("primary_model"):
        primary_model = str(raw["primary_model"]).strip()
        field_sources["primary_model"] = "config_file"

    profile = DEFAULT_BOOTSTRAP_PROFILE
    field_sources["profile"] = "default"
    env_profile = _text(merged, _PROFILE_ENV)
    if env_profile:
        profile = env_profile
        field_sources["profile"] = env_source(_PROFILE_ENV)
    if raw.get("profile"):
        profile = str(raw["profile"]).strip()
        field_sources["profile"] = "config_file"

    log_path = DEFAULT_BOOTSTRAP_LOG_PATH
    field_sources["log_path"] = "default"
    env_log = _text(merged, _LOG_PATH_ENV)
    if env_log:
        log_path = env_log
        field_sources["log_path"] = env_source(_LOG_PATH_ENV)
    if raw.get("log_path"):
        log_path = str(raw["log_path"]).strip()
        field_sources["log_path"] = "config_file"

    if field_sources["primary_model"] == "default":
        # A defaulted identity is not a refusal here (the caller may never route
        # to it), but it must never be silent: IntelligenceService returns
        # primary_model as its tier-0 / no-offload-match decision, so an
        # unintended model can end up executing work.
        diagnostics.append(
            BootstrapDiagnostic(
                code="default_primary_model",
                diagnostic_class="configuration",
                field="primary_model",
                source="default",
                detail=(
                    f"no primary_model from any canonical source; using the built-in "
                    f"default {primary_model!r}, which is returned as the tier-0 / "
                    f"no-offload-match routing decision"
                ),
                remediation=(f"set 'primary_model' in {path}, or export {_PRIMARY_MODEL_ENV}"),
                fatal=False,
            )
        )

    providers = _providers_from_config(raw, config_path=path)
    if providers:
        field_sources["providers"] = "config_file"

    env_gateway = _text(merged, _GATEWAY_ENV)
    if env_gateway:
        invalid = _validate_url(
            _normalize_gateway_url(env_gateway),
            field_name=_GATEWAY_ENV,
            source=env_source(_GATEWAY_ENV),
        )
        if invalid is not None:
            raise BootstrapError("bootstrap_configuration_invalid", (invalid,))
        binding = ProviderBinding(
            name=_GATEWAY_PROVIDER_NAME,
            base_url=_openai_base(env_gateway),
            source=env_source(_GATEWAY_ENV),
            api_key_env=_GATEWAY_KEY_ENV,
        )
        existing = providers.get(_GATEWAY_PROVIDER_NAME)
        if existing is None:
            providers[_GATEWAY_PROVIDER_NAME] = binding
            field_sources.setdefault("providers", binding.source)
        elif existing.base_url != binding.base_url:
            # Config wins for the provider map, matching shipped behaviour; the
            # disagreement is still reported so it is never silent.
            diagnostics.append(
                BootstrapDiagnostic(
                    code="precedence_conflict",
                    diagnostic_class="configuration",
                    field=f"providers.{_GATEWAY_PROVIDER_NAME}.base_url",
                    source="config_file",
                    detail=(
                        f"config file sets {redact_url(existing.base_url)!r} while "
                        f"{_GATEWAY_ENV} sets {redact_url(binding.base_url)!r}; "
                        f"the config file wins for the provider map"
                    ),
                    remediation=(
                        f"align {_GATEWAY_ENV} with {path}, or remove the "
                        f"'{_GATEWAY_PROVIDER_NAME}' provider from {path}"
                    ),
                    fatal=False,
                )
            )

    if not providers:
        if not allow_default_providers:
            raise BootstrapError(
                "bootstrap_configuration_incomplete",
                (
                    BootstrapDiagnostic(
                        code="no_provider_configuration",
                        diagnostic_class="configuration",
                        field="providers",
                        source=None,
                        detail=(
                            f"no provider configuration from any canonical source "
                            f"({_GATEWAY_ENV} unset; 'providers' absent in {path})"
                        ),
                        remediation=_REMEDIATION_PROVIDERS.format(config_path=path),
                    ),
                ),
            )
        defaults = DEFAULT_LOCAL_PROVIDERS if default_providers is None else dict(default_providers)
        named = ", ".join(f"{name}={url}" for name, url in sorted(defaults.items()))
        if require_authoritative or profile == PRODUCTION_PROFILE:
            raise BootstrapError(
                "bootstrap_configuration_incomplete",
                (
                    BootstrapDiagnostic(
                        code="default_providers_forbidden",
                        diagnostic_class="configuration",
                        field="providers",
                        source=None,
                        detail=(
                            f"no provider configuration from any canonical source, and built-in "
                            f"local defaults ({named}) are not an authoritative provider set "
                            f"(profile={profile}, "
                            f"require_authoritative={str(require_authoritative).lower()})"
                        ),
                        remediation=_REMEDIATION_PROVIDERS.format(config_path=path),
                    ),
                ),
            )
        for name, url in defaults.items():
            providers[name] = ProviderBinding(name=name, base_url=url, source="default")
        field_sources["providers"] = "default"
        diagnostics.append(
            BootstrapDiagnostic(
                code="default_provider_fallback",
                diagnostic_class="configuration",
                field="providers",
                source="default",
                detail=(
                    f"no provider configuration from any canonical source; using built-in "
                    f"local defaults ({named}). This set is for interactive local use only "
                    f"and is refused under profile {PRODUCTION_PROFILE!r} or when the caller "
                    f"requires authoritative execution"
                ),
                remediation=_REMEDIATION_PROVIDERS.format(config_path=path),
                fatal=False,
            )
        )

    # gateway_url precedence is environment-first, matching shipped behaviour.
    gateway_url: str | None = None
    if env_gateway:
        gateway_url = _normalize_gateway_url(env_gateway)
        field_sources["gateway_url"] = env_source(_GATEWAY_ENV)
    elif raw.get("gateway_url"):
        candidate = str(raw["gateway_url"]).strip()
        invalid = _validate_url(candidate, field_name="gateway_url", source="config_file")
        if invalid is not None:
            raise BootstrapError("bootstrap_configuration_invalid", (invalid,))
        gateway_url = _normalize_gateway_url(candidate)
        field_sources["gateway_url"] = "config_file"

    gateway_providers = sorted(n for n, b in providers.items() if _looks_like_gateway(b))
    gateway_required = bool(gateway_providers)
    if gateway_required and gateway_url is None:
        gateway_url = _normalize_gateway_url(providers[gateway_providers[0]].base_url)
        field_sources["gateway_url"] = providers[gateway_providers[0]].source

    for binding in providers.values():
        if binding.api_key_env and not _text(merged, binding.api_key_env):
            diagnostics.append(
                BootstrapDiagnostic(
                    code="credential_env_missing",
                    diagnostic_class="configuration",
                    field=binding.api_key_env,
                    source=None,
                    detail=(
                        f"provider {binding.name!r} declares api_key_env "
                        f"{binding.api_key_env} but it is unset in the environment "
                        f"and in the credential store"
                    ),
                    remediation=(
                        f"export {binding.api_key_env}, or run "
                        f"'verdict credentials set {binding.api_key_env}'"
                    ),
                    fatal=False,
                )
            )

    return BootstrapResult(
        primary_model=primary_model,
        providers=dict(sorted(providers.items())),
        gateway_required=gateway_required,
        gateway_url=gateway_url,
        profile=profile,
        log_path=log_path,
        config_path=path,
        config_present=present,
        field_sources=field_sources,
        diagnostics=tuple(diagnostics),
    )


def verify_gateway_reachable(result: BootstrapResult, *, probe: Any) -> BootstrapDiagnostic | None:
    """Classify gateway inventory/health using a caller-supplied probe.

    ``probe`` is called as ``probe(gateway_url)`` and must return a
    ``GatewayProbeResult`` (or any object with ``reachable`` / ``detail``). This
    function never opens a socket and never starts or stops a gateway; the
    lifecycle owner consumes ``gateway_required`` plus this diagnostic.

    Returns ``None`` when no gateway is required or the gateway answered, and a
    ``gateway_health`` diagnostic otherwise — never a ``configuration`` one.
    """
    if not result.gateway_required:
        return None
    if not result.gateway_url:
        return BootstrapDiagnostic(
            code="gateway_required_but_absent",
            diagnostic_class="gateway_health",
            field="gateway_url",
            source=None,
            detail="a gateway provider is configured but no gateway URL resolved",
            remediation=f"set {_GATEWAY_ENV}, or add 'gateway_url' to the routing config",
        )
    shown = redact_url(result.gateway_url)
    try:
        outcome = probe(result.gateway_url)
    except Exception as exc:
        return BootstrapDiagnostic(
            code="gateway_unreachable",
            diagnostic_class="gateway_health",
            field="gateway_url",
            source=result.source_of("gateway_url"),
            detail=f"gateway health probe for {shown} failed: {redact_url(str(exc))}",
            remediation=f"start the gateway at {shown}, or run 'verdict detect'",
        )
    if getattr(outcome, "reachable", False):
        return None
    detail = str(getattr(outcome, "detail", "") or "no health response")
    return BootstrapDiagnostic(
        code="gateway_unreachable",
        diagnostic_class="gateway_health",
        field="gateway_url",
        source=result.source_of("gateway_url"),
        detail=f"gateway at {shown} is not healthy: {redact_url(detail)}",
        remediation=f"start the gateway at {shown}, or run 'verdict detect'",
    )


def describe_bootstrap_failure(exc: BootstrapError) -> str:
    """Deterministic multi-line operator report for a bootstrap refusal."""
    lines = [f"{exc.reason_code} ({exc.diagnostic_class})"]
    lines.extend(f"  - {d.describe()}" for d in exc.diagnostics)
    return "\n".join(lines)
