"""Local live capacity discovery (capacity evidence follow-up).

Evidence only: detect CLIs / config / auth presence, optionally shell official
CLIs with fixed argv + bounded timeout, or load aggregator JSON from known
paths. Never chooses routes. Never returns secrets/tokens/cookies.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from verdict.capacity_aggregator import AggregatorJsonCapacityAdapter
from verdict.capacity_models import (
    CapacityFailureClass,
    CapacityObservationError,
    CapacitySignal,
    CapacitySnapshot,
    ConnectionIdentity,
    DiagnoseReport,
    EvidenceAuthority,
    RefreshPolicy,
    SourceKind,
    _reject_secrets,
    _utc,
)

AuthPresence = Literal["yes", "no", "unknown"]
Qualification = Literal["CERTIFIED", "PARTIAL", "UNSUPPORTED", "ABSENT", "NEEDS_OWNER"]

_DEFAULT_TIMEOUT_SECONDS = 8
_MAX_CLI_OUTPUT_BYTES = 256_000
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _fingerprint(value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    return f"sha12-{digest}"


def _which(name: str, *, path_resolver: Callable[[str], str | None] | None = None) -> str | None:
    resolver = path_resolver or shutil.which
    return resolver(name)


def _env_nonempty(name: str, env: Mapping[str, str]) -> bool:
    return bool(str(env.get(name, "")).strip())


def _dotenv_nonempty(path: Path, name: str) -> bool:
    if not path.is_file():
        return False
    try:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, raw = stripped.partition("=")
            if key.strip() != name:
                continue
            value = raw.strip().strip("'").strip('"')
            return bool(value)
    except OSError:
        return False
    return False


def _read_json_object(path: Path) -> Mapping[str, Any] | None:
    try:
        raw = path.read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    return payload if isinstance(payload, Mapping) else None


def _run_fixed_argv(
    argv: Sequence[str],
    *,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> tuple[int, str]:
    """Run a fixed argv list (no shell). Truncate/redact output for diagnose only."""

    active = runner or subprocess.run
    try:
        completed = active(
            list(argv), capture_output=True, text=True, timeout=timeout_seconds, check=False
        )
    except FileNotFoundError:
        return 127, "binary_not_found"
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    except OSError as exc:
        return 1, f"os_error:{type(exc).__name__}"

    combined = f"{completed.stdout or ''}{completed.stderr or ''}"
    if len(combined.encode("utf-8", errors="ignore")) > _MAX_CLI_OUTPUT_BYTES:
        combined = combined[: _MAX_CLI_OUTPUT_BYTES // 2]
    cleaned = _ANSI_RE.sub("", combined)
    # Never retain credential-shaped values in diagnose payloads.
    cleaned = re.sub(
        r"(?i)(api[_-]?key|token|secret|authorization|cookie|password|bearer)\s*[:=]\s*\S+",
        r"\1=<redacted>",
        cleaned,
    )
    cleaned = re.sub(r"sk-[A-Za-z0-9_-]{8,}", "sk-<redacted>", cleaned)
    return int(completed.returncode), cleaned.strip()


def _extract_json_payload(text: str) -> Any | None:
    cleaned = _ANSI_RE.sub("", text)
    for opener, closer in (("[", "]"), ("{", "}")):
        start = cleaned.find(opener)
        end = cleaned.rfind(closer)
        if start >= 0 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                continue
    return None


@dataclass(frozen=True)
class LocalCapacityProbe:
    """One certification-matrix row (no secrets)."""

    adapter_id: str
    installed: bool
    configured: bool
    auth: AuthPresence
    discover: bool
    observe: bool
    diagnose: bool
    qualification: Qualification
    failure_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "adapter_id": self.adapter_id,
            "installed": self.installed,
            "configured": self.configured,
            "auth": self.auth,
            "discover": self.discover,
            "observe": self.observe,
            "diagnose": self.diagnose,
            "qualification": self.qualification,
            "failure_reason": self.failure_reason,
        }


class LiveLocalCapacityAdapter:
    """Presence/diagnose adapter for one local capacity source.

    ``observe_capacity`` remains explicit unknown/unsupported unless an
    aggregator document path is supplied (never fabricates remaining %).
    """

    def __init__(
        self,
        adapter_id: str,
        *,
        provider_id: str,
        source_kind: SourceKind,
        authority: EvidenceAuthority,
        installed: bool,
        configured: bool,
        auth: AuthPresence,
        account_id: str | None = None,
        access_mode: str = "unknown",
        gateway_id: str | None = None,
        aggregator_path: Path | None = None,
        diagnose_details: Mapping[str, Any] | None = None,
        needs_owner: str | None = None,
    ) -> None:
        self._adapter_id = adapter_id
        self._provider_id = provider_id
        self._source_kind = source_kind
        self._authority = authority
        self._installed = installed
        self._configured = configured
        self._auth = auth
        self._account_id = account_id
        self._access_mode = access_mode
        self._gateway_id = gateway_id
        self._aggregator_path = aggregator_path
        details = dict(diagnose_details or {})
        _reject_secrets(details, "live_diagnose")
        self._diagnose_details = details
        self._needs_owner = needs_owner

    @property
    def adapter_id(self) -> str:
        return self._adapter_id

    def discover(self) -> Sequence[ConnectionIdentity]:
        if not self._installed or not self._configured or self._auth == "no":
            return ()
        if not self._account_id:
            return ()
        return (
            ConnectionIdentity(
                provider_id=self._provider_id,
                account_id=self._account_id,
                adapter_id=self._adapter_id,
                access_mode=self._access_mode,  # type: ignore[arg-type]
                gateway_id=self._gateway_id,
            ),
        )

    def capabilities(self) -> Mapping[CapacitySignal, bool]:
        has_doc = self._aggregator_path is not None and self._aggregator_path.is_file()
        return {
            CapacitySignal.QUOTA_WINDOWS: has_doc,
            CapacitySignal.BALANCES: has_doc,
            CapacitySignal.RESET_TIMES: has_doc,
            CapacitySignal.COOLDOWNS: False,
            CapacitySignal.RETRY_AFTER: False,
            CapacitySignal.SHARED_POOLS: has_doc,
            CapacitySignal.HEALTH: True,
        }

    def refresh_policy(self) -> RefreshPolicy:
        return RefreshPolicy(ttl_seconds=60, prefer_reset_at=True, honor_retry_after=False)

    def diagnose(self) -> DiagnoseReport:
        available = self._installed and self._configured and self._auth != "no"
        # Owner-gated auth wins over config gaps so certification surfaces MFA/login.
        if self._needs_owner:
            status = "needs_owner"
        elif not self._installed:
            status = "not_installed"
        elif self._auth == "no":
            status = "auth_missing"
        elif not self._configured:
            status = "not_configured"
        elif self._auth == "unknown":
            status = "auth_unknown"
        elif self._aggregator_path and self._aggregator_path.is_file():
            status = "ok"
        else:
            status = "observe_unsupported"
        details = {
            "installed": self._installed,
            "configured": self._configured,
            "auth": self._auth,
            "identity_count": len(self.discover()),
            **self._diagnose_details,
        }
        if self._needs_owner:
            details["needs_owner"] = self._needs_owner
        if self._aggregator_path is not None:
            details["aggregator_path_present"] = self._aggregator_path.is_file()
        return DiagnoseReport(
            adapter_id=self._adapter_id, available=available, status=status, details=details
        )

    def observe_capacity(
        self, identity: ConnectionIdentity | None = None, *, now: datetime | None = None
    ) -> CapacitySnapshot:
        moment = _utc(now)
        discovered = self.discover()
        if identity is None:
            if discovered:
                identity = discovered[0]
            else:
                identity = ConnectionIdentity(
                    provider_id=self._provider_id,
                    account_id="unavailable",
                    adapter_id=self._adapter_id,
                    access_mode="unknown",
                    gateway_id=self._gateway_id,
                )

        if self._aggregator_path is not None and self._aggregator_path.is_file():
            try:
                return AggregatorJsonCapacityAdapter.from_path(
                    self._aggregator_path, adapter_id=self._adapter_id
                ).observe_capacity(identity, now=moment)
            except Exception as exc:
                return CapacitySnapshot(
                    identity=identity,
                    source_kind="aggregator",
                    authority=EvidenceAuthority.AGGREGATOR,
                    observed_at=moment,
                    errors=(
                        CapacityObservationError(
                            failure_class=CapacityFailureClass.PARSE_ERROR,
                            message=f"aggregator read failed: {type(exc).__name__}",
                        ),
                    ),
                    notes=("live_local", "aggregator_path"),
                )

        failure = CapacityFailureClass.UNSUPPORTED
        message = "live quota observation unsupported without official usage CLI or aggregator JSON"
        if self._auth == "no":
            failure = CapacityFailureClass.AUTH_EXPIRED
            message = "auth missing for live capacity observation"
        elif not self._installed:
            message = "binary not installed"
        elif self._needs_owner:
            failure = CapacityFailureClass.UNSUPPORTED
            message = f"needs owner action: {self._needs_owner}"

        return CapacitySnapshot(
            identity=identity,
            source_kind=self._source_kind,
            authority=self._authority,
            observed_at=moment,
            fresh_until=moment + timedelta(seconds=30),
            confidence=None,
            health="unknown",
            errors=(CapacityObservationError(failure_class=failure, message=message),),
            notes=("live_local", "unknown_remaining"),
        )


class OmniRouteLiveCapacityAdapter:
    """Optional OmniRoute gateway evidence via official CLI (never routing)."""

    def __init__(
        self,
        *,
        binary: str | None = None,
        home: Path | None = None,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        self._adapter_id = "gateway.omniroute.live"
        self._binary = binary
        self._home = home or (Path.home() / ".omniroute")
        self._timeout_seconds = timeout_seconds
        self._runner = runner

    @property
    def adapter_id(self) -> str:
        return self._adapter_id

    def discover(self) -> Sequence[ConnectionIdentity]:
        if not self._binary or not self._home.is_dir():
            return ()
        return (
            ConnectionIdentity(
                provider_id="omniroute",
                account_id="gateway-local",
                adapter_id=self._adapter_id,
                access_mode="upstream_proxy",
                gateway_id="omniroute",
            ),
        )

    def capabilities(self) -> Mapping[CapacitySignal, bool]:
        return {
            CapacitySignal.QUOTA_WINDOWS: False,
            CapacitySignal.BALANCES: False,
            CapacitySignal.RESET_TIMES: False,
            CapacitySignal.COOLDOWNS: False,
            CapacitySignal.RETRY_AFTER: False,
            CapacitySignal.SHARED_POOLS: False,
            CapacitySignal.HEALTH: True,
        }

    def refresh_policy(self) -> RefreshPolicy:
        return RefreshPolicy(ttl_seconds=30, prefer_reset_at=False, honor_retry_after=False)

    def diagnose(self) -> DiagnoseReport:
        if not self._binary:
            return DiagnoseReport(
                adapter_id=self._adapter_id,
                available=False,
                status="not_installed",
                details={"installed": False},
            )
        code, output = _run_fixed_argv(
            [self._binary, "quota", "status"],
            timeout_seconds=self._timeout_seconds,
            runner=self._runner,
        )
        payload = _extract_json_payload(output)
        providers = {}
        gateway = None
        if isinstance(payload, Mapping):
            gateway = payload.get("gateway")
            providers_block = payload.get("providers")
            if isinstance(providers_block, Mapping):
                providers = {
                    "configured": providers_block.get("configured"),
                    "active": providers_block.get("active"),
                    "healthy": providers_block.get("healthy"),
                }
        return DiagnoseReport(
            adapter_id=self._adapter_id,
            available=code == 0 and gateway is not None,
            status="ok" if code == 0 else f"cli_exit_{code}",
            details={
                "installed": True,
                "configured": self._home.is_dir(),
                "cli_exit": code,
                "gateway": gateway,
                "providers": providers,
                "quota_windows": False,
                "note": "evidence_only_not_routing",
            },
        )

    def observe_capacity(
        self, identity: ConnectionIdentity | None = None, *, now: datetime | None = None
    ) -> CapacitySnapshot:
        moment = _utc(now)
        if identity is None:
            discovered = self.discover()
            identity = (
                discovered[0]
                if discovered
                else ConnectionIdentity(
                    provider_id="omniroute",
                    account_id="unavailable",
                    adapter_id=self._adapter_id,
                    access_mode="upstream_proxy",
                    gateway_id="omniroute",
                )
            )
        if not self._binary:
            return CapacitySnapshot(
                identity=identity,
                source_kind="gateway",
                authority=EvidenceAuthority.GATEWAY_NATIVE,
                observed_at=moment,
                errors=(
                    CapacityObservationError(
                        failure_class=CapacityFailureClass.UNSUPPORTED,
                        message="omniroute binary not installed",
                    ),
                ),
                notes=("live_local", "optional_gateway"),
            )

        code, output = _run_fixed_argv(
            [self._binary, "quota", "--json"],
            timeout_seconds=self._timeout_seconds,
            runner=self._runner,
        )
        payload = _extract_json_payload(output)
        # Remaining quota is often absent ("No quota data") — keep unknown.
        if isinstance(payload, Mapping) and "error" in payload:
            status_code, status_out = _run_fixed_argv(
                [self._binary, "quota", "status"],
                timeout_seconds=self._timeout_seconds,
                runner=self._runner,
            )
            status_payload = _extract_json_payload(status_out)
            health = None
            if isinstance(status_payload, Mapping):
                health = (
                    None
                    if status_payload.get("gateway") is None
                    else str(status_payload.get("gateway"))
                )
            return CapacitySnapshot(
                identity=identity,
                source_kind="gateway",
                authority=EvidenceAuthority.GATEWAY_NATIVE,
                observed_at=moment,
                fresh_until=moment + timedelta(seconds=30),
                confidence=None,
                health=health,
                errors=(
                    CapacityObservationError(
                        failure_class=CapacityFailureClass.UNSUPPORTED,
                        message=str(payload.get("error") or "omniroute quota unsupported"),
                    ),
                ),
                notes=(
                    "live_local",
                    "optional_gateway",
                    "unknown_remaining",
                    f"status_exit_{status_code}",
                ),
            )

        return CapacitySnapshot(
            identity=identity,
            source_kind="gateway",
            authority=EvidenceAuthority.GATEWAY_NATIVE,
            observed_at=moment,
            health="unknown",
            errors=(
                CapacityObservationError(
                    failure_class=CapacityFailureClass.UNSUPPORTED,
                    message=f"omniroute quota parse unsupported (exit {code})",
                ),
            ),
            notes=("live_local", "optional_gateway", "unknown_remaining"),
        )


def _aggregator_candidates(home: Path, env: Mapping[str, str]) -> list[Path]:
    paths: list[Path] = []
    explicit = env.get("VERDICT_CAPACITY_JSON", "").strip()
    if explicit:
        paths.append(Path(explicit).expanduser())
    paths.extend(
        [
            home / ".config" / "verdict" / "capacity.json",
            home / ".config" / "verdict" / "capacity" / "aggregator.json",
            home / ".local" / "share" / "codexbar" / "usage.json",
            home / ".config" / "codexbar" / "usage.json",
            home / ".quota-cli" / "capacity.json",
        ]
    )
    return paths


def _first_existing(paths: Sequence[Path]) -> Path | None:
    for path in paths:
        if path.is_file():
            return path
    return None


def discover_local_adapters(
    *,
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
    path_resolver: Callable[[str], str | None] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    include_omniroute: bool = True,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
) -> tuple[Any, ...]:
    """Return live adapters for sources present on this machine (evidence only)."""

    root = home or Path.home()
    environ = env if env is not None else os.environ
    adapters: list[Any] = []

    # Codex / OpenAI ChatGPT auth file
    codex_bin = _which("codex", path_resolver=path_resolver)
    codex_auth = root / ".codex" / "auth.json"
    codex_payload = _read_json_object(codex_auth) if codex_auth.is_file() else None
    codex_account = None
    codex_auth_state: AuthPresence = "no"
    if isinstance(codex_payload, Mapping):
        tokens = codex_payload.get("tokens")
        if isinstance(tokens, Mapping) and tokens.get("account_id"):
            codex_account = _fingerprint(str(tokens["account_id"]))
            codex_auth_state = "yes"
        elif codex_payload.get("OPENAI_API_KEY") or (
            isinstance(tokens, Mapping)
            and (tokens.get("access_token") or tokens.get("refresh_token"))
        ):
            # Key/token present but no stable account id — fingerprint path only.
            codex_account = _fingerprint(f"codex-auth:{codex_auth}")
            codex_auth_state = "yes"
    adapters.append(
        LiveLocalCapacityAdapter(
            "direct.codex.live",
            provider_id="openai",
            source_kind="direct_provider",
            authority=EvidenceAuthority.OFFICIAL_CLI,
            installed=bool(codex_bin),
            configured=codex_auth.is_file(),
            auth=codex_auth_state,
            account_id=codex_account,
            access_mode="oauth" if codex_auth_state == "yes" else "unknown",
            diagnose_details={
                "binary_present": bool(codex_bin),
                "auth_path_present": codex_auth.is_file(),
                "auth_mode_present": bool(
                    isinstance(codex_payload, Mapping) and codex_payload.get("auth_mode")
                ),
            },
        )
    )

    # Claude Code
    claude_bin = _which("claude", path_resolver=path_resolver)
    claude_settings = (root / ".claude" / "settings.json").is_file() or (
        root / ".claude.json"
    ).is_file()
    claude_auth: AuthPresence = "unknown"
    claude_account = None
    claude_needs_owner = None
    if claude_bin:
        code, output = _run_fixed_argv(
            [claude_bin, "auth", "status"], timeout_seconds=timeout_seconds, runner=runner
        )
        payload = _extract_json_payload(output)
        if isinstance(payload, Mapping) and "loggedIn" in payload:
            if payload.get("loggedIn") is True:
                claude_auth = "yes"
                claude_account = _fingerprint("claude-logged-in")
            else:
                claude_auth = "no"
                claude_needs_owner = "interactive_claude_auth_login"
        elif code == 124:
            claude_auth = "unknown"
            claude_needs_owner = "claude_auth_status_timeout"
    adapters.append(
        LiveLocalCapacityAdapter(
            "direct.claude.live",
            provider_id="anthropic",
            source_kind="direct_provider",
            authority=EvidenceAuthority.OFFICIAL_CLI,
            installed=bool(claude_bin),
            configured=claude_settings,
            auth=claude_auth,
            account_id=claude_account,
            access_mode="oauth" if claude_auth == "yes" else "unknown",
            needs_owner=claude_needs_owner,
            diagnose_details={
                "binary_present": bool(claude_bin),
                "settings_present": claude_settings,
            },
        )
    )

    # Cursor Agent
    cursor_bin = _which("cursor-agent", path_resolver=path_resolver) or _which(
        "agent", path_resolver=path_resolver
    )
    cursor_cfg = root / ".cursor" / "cli-config.json"
    cursor_payload = _read_json_object(cursor_cfg) if cursor_cfg.is_file() else None
    cursor_auth: AuthPresence = "no"
    cursor_account = None
    if isinstance(cursor_payload, Mapping):
        auth_info = cursor_payload.get("authInfo")
        if isinstance(auth_info, Mapping) and (auth_info.get("userId") or auth_info.get("authId")):
            cursor_auth = "yes"
            cursor_account = _fingerprint(str(auth_info.get("userId") or auth_info.get("authId")))
    if cursor_bin and cursor_auth == "no":
        # Fall back to `cursor-agent status` (may print email — we only keep exit code).
        code, _output = _run_fixed_argv(
            [cursor_bin, "status"], timeout_seconds=timeout_seconds, runner=runner
        )
        if code == 0:
            cursor_auth = "yes"
            cursor_account = cursor_account or _fingerprint("cursor-agent-status")
    adapters.append(
        LiveLocalCapacityAdapter(
            "direct.cursor.live",
            provider_id="cursor",
            source_kind="direct_provider",
            authority=EvidenceAuthority.OFFICIAL_CLI,
            installed=bool(cursor_bin),
            configured=cursor_cfg.is_file(),
            auth=cursor_auth,
            account_id=cursor_account,
            access_mode="oauth" if cursor_auth == "yes" else "unknown",
            diagnose_details={
                "binary_present": bool(cursor_bin),
                "cli_config_present": cursor_cfg.is_file(),
            },
        )
    )

    # Hermes harness (credential pool / env — not a router)
    hermes_bin = _which("hermes", path_resolver=path_resolver)
    hermes_auth_path = root / ".hermes" / "auth.json"
    hermes_env = root / ".hermes" / ".env"
    hermes_configured = hermes_auth_path.is_file() or hermes_env.is_file()
    hermes_payload = _read_json_object(hermes_auth_path) if hermes_auth_path.is_file() else None
    pool_providers: list[str] = []
    if isinstance(hermes_payload, Mapping):
        pool = hermes_payload.get("credential_pool")
        if isinstance(pool, Mapping):
            for name, entries in pool.items():
                if isinstance(entries, list) and entries:
                    pool_providers.append(str(name))
    hermes_auth: AuthPresence = (
        "yes" if pool_providers else ("unknown" if hermes_configured else "no")
    )
    hermes_account = (
        _fingerprint("hermes-pool:" + ",".join(sorted(pool_providers))) if pool_providers else None
    )
    adapters.append(
        LiveLocalCapacityAdapter(
            "harness.hermes.live",
            provider_id="hermes",
            source_kind="gateway",
            authority=EvidenceAuthority.OFFICIAL_CLI,
            installed=bool(hermes_bin),
            configured=hermes_configured,
            auth=hermes_auth,
            account_id=hermes_account,
            access_mode="api_key" if hermes_auth == "yes" else "unknown",
            gateway_id="hermes",
            diagnose_details={
                "binary_present": bool(hermes_bin),
                "auth_path_present": hermes_auth_path.is_file(),
                "pool_provider_count": len(pool_providers),
                "pool_providers": sorted(pool_providers),
            },
        )
    )

    # OpenRouter — env or Hermes dotenv presence only (never read key value into snapshot)
    openrouter_env = _env_nonempty("OPENROUTER_API_KEY", environ) or _dotenv_nonempty(
        hermes_env, "OPENROUTER_API_KEY"
    )
    adapters.append(
        LiveLocalCapacityAdapter(
            "gateway.openrouter.live",
            provider_id="openrouter",
            source_kind="gateway",
            authority=EvidenceAuthority.PROVIDER_API,
            installed=True,  # API-key surface; no dedicated CLI required
            configured=openrouter_env or (root / ".config" / "openrouter").exists(),
            auth="yes" if openrouter_env else "no",
            account_id=_fingerprint("openrouter-env") if openrouter_env else None,
            access_mode="api_key" if openrouter_env else "unknown",
            gateway_id="openrouter",
            diagnose_details={
                "env_key_present": openrouter_env,
                "config_dir_present": (root / ".config" / "openrouter").exists(),
            },
        )
    )

    # LiteLLM — only when module/CLI present
    litellm_bin = _which("litellm", path_resolver=path_resolver)
    adapters.append(
        LiveLocalCapacityAdapter(
            "gateway.litellm.live",
            provider_id="litellm",
            source_kind="gateway",
            authority=EvidenceAuthority.GATEWAY_NATIVE,
            installed=bool(litellm_bin),
            configured=(root / ".config" / "litellm").exists()
            or (root / ".litellm").exists()
            or _env_nonempty("LITELLM_API_KEY", environ),
            auth="yes"
            if _env_nonempty("LITELLM_API_KEY", environ)
            else ("unknown" if litellm_bin else "no"),
            account_id=None,
            access_mode="api_key",
            gateway_id="litellm",
            diagnose_details={"binary_present": bool(litellm_bin)},
        )
    )

    # Aggregator JSON (quota-cli / CodexBar-compatible)
    agg_path = _first_existing(_aggregator_candidates(root, environ))
    if agg_path is not None:
        try:
            adapters.append(
                AggregatorJsonCapacityAdapter.from_path(agg_path, adapter_id="aggregator.json.live")
            )
        except Exception:
            adapters.append(
                LiveLocalCapacityAdapter(
                    "aggregator.json.live",
                    provider_id="aggregator",
                    source_kind="aggregator",
                    authority=EvidenceAuthority.AGGREGATOR,
                    installed=True,
                    configured=True,
                    auth="unknown",
                    account_id=_fingerprint(str(agg_path)),
                    aggregator_path=agg_path,
                    diagnose_details={"path_present": True, "parse": "invalid"},
                )
            )
    else:
        adapters.append(
            LiveLocalCapacityAdapter(
                "aggregator.json.live",
                provider_id="aggregator",
                source_kind="aggregator",
                authority=EvidenceAuthority.AGGREGATOR,
                installed=bool(
                    _which("quota-cli", path_resolver=path_resolver)
                    or _which("codexbar", path_resolver=path_resolver)
                ),
                configured=False,
                auth="no",
                account_id=None,
                diagnose_details={
                    "quota_cli_present": bool(_which("quota-cli", path_resolver=path_resolver)),
                    "codexbar_present": bool(_which("codexbar", path_resolver=path_resolver)),
                },
            )
        )

    if include_omniroute:
        omni_bin = _which("omniroute", path_resolver=path_resolver)
        omni_home = root / ".omniroute"
        if omni_bin or omni_home.is_dir():
            adapters.append(
                OmniRouteLiveCapacityAdapter(
                    binary=omni_bin, home=omni_home, timeout_seconds=timeout_seconds, runner=runner
                )
            )

    # Only return adapters that are at least installed or configured (skip pure absents
    # except we keep litellm/aggregator rows for diagnose visibility when partially set).
    return tuple(adapters)


def probe_local_capacity_sources(
    *,
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
    path_resolver: Callable[[str], str | None] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    include_omniroute: bool = True,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
) -> tuple[LocalCapacityProbe, ...]:
    """Build the certification matrix (no secrets)."""

    adapters = discover_local_adapters(
        home=home,
        env=env,
        path_resolver=path_resolver,
        runner=runner,
        include_omniroute=include_omniroute,
        timeout_seconds=timeout_seconds,
    )
    rows: list[LocalCapacityProbe] = []
    for adapter in adapters:
        report = adapter.diagnose()
        details = dict(report.details)
        installed = bool(details.get("installed", report.available))
        configured = bool(details.get("configured", report.available))
        auth_raw = details.get("auth")
        auth: AuthPresence
        if auth_raw == "yes" or auth_raw == "no" or auth_raw == "unknown":
            auth = auth_raw
        elif report.available:
            auth = "yes"
        else:
            auth = "unknown"
        identities = tuple(adapter.discover())
        discover_ok = bool(identities)
        snap = adapter.observe_capacity(now=_utc())
        observe_ok = bool(snap.pools or snap.balances) and not any(
            err.failure_class
            in {
                CapacityFailureClass.UNSUPPORTED,
                CapacityFailureClass.AUTH_EXPIRED,
                CapacityFailureClass.PARSE_ERROR,
            }
            for err in snap.errors
        )
        diagnose_ok = report.status not in {"not_installed"} and "cli_exit_124" not in report.status

        if not installed and not configured:
            qualification: Qualification = "ABSENT"
            reason = "not installed or configured on this machine"
        elif details.get("needs_owner") or report.status == "needs_owner":
            qualification = "NEEDS_OWNER"
            reason = str(details.get("needs_owner") or "interactive auth required")
        elif observe_ok and discover_ok and diagnose_ok:
            qualification = "CERTIFIED"
            reason = None
        elif discover_ok and diagnose_ok:
            qualification = "PARTIAL"
            reason = (
                snap.errors[0].message
                if snap.errors
                else "discover/diagnose ok; live quota observe unsupported"
            )
        elif installed:
            qualification = "UNSUPPORTED"
            reason = report.status
        else:
            qualification = "ABSENT"
            reason = report.status

        rows.append(
            LocalCapacityProbe(
                adapter_id=adapter.adapter_id,
                installed=installed,
                configured=configured,
                auth=auth,
                discover=discover_ok,
                observe=observe_ok,
                diagnose=diagnose_ok,
                qualification=qualification,
                failure_reason=reason,
            )
        )
    return tuple(rows)


__all__ = [
    "LiveLocalCapacityAdapter",
    "LocalCapacityProbe",
    "OmniRouteLiveCapacityAdapter",
    "discover_local_adapters",
    "probe_local_capacity_sources",
]
