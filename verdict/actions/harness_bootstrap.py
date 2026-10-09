"""Thin bootstrap actions. Discovery reads paths; never runs binaries or helpers.

Pure previews accept injected documents. Mutation actions require the exact preview
object and digest from the separately confirmed consumer. Claude has no apply API.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from verdict.actions.base import ActionResult
from verdict.actions.verified_models import utc_now
from verdict.harness_prime_compat import PrimeCompatibilityContext, exact_token
from verdict.harness_prime_selection import (
    PrimeDependencies,
    PrimeSelectionError,
    SelectionPaths,
    apply_selection,
    byte_digest,
    load_receipt,
    load_settings,
    preview_restore,
    preview_selection,
    restore_selection,
)
from verdict.orchestration.prime_settings import default_prime_agent_dir


@dataclass(frozen=True)
class PrimeReadAdapter:
    """Read-only operator discovery with injectable release/binary facts for tests."""

    paths: SelectionPaths
    gateway: str
    which: Callable[[str], str | None] | None = None

    def dependencies(self) -> PrimeDependencies:
        from verdict.harness_prime import discover

        report = discover(prime_home=self.paths.agent_dir, which=self.which)
        binary = Path(report.binary_path).resolve() if report.binary_path else None
        release = None
        digest = ""
        if binary is not None and binary.is_file():
            match = re.fullmatch(r"(\d+\.\d+\.\d+)-.+", binary.parent.name)
            release = match[1] if match else None
            digest = byte_digest(binary.read_bytes())
        registry = load_settings(self.paths.models)
        provider = registry.value.get("providers", {}).get("omniroute", {})
        project_digest = None
        override = False
        if self.paths.project_settings is not None and self.paths.project_settings.exists():
            project = load_settings(self.paths.project_settings)
            project_digest = project.pre_digest
            override = "enabledModels" in project.value
        endpoint = self.gateway.rstrip("/")
        if not endpoint.endswith("/v1"):
            endpoint += "/v1"
        context = PrimeCompatibilityContext(
            agent_dir=self.paths.agent_dir,
            installed=report.installed,
            release=release,
            binary_digest=digest,
            gateway_endpoint=endpoint,
            evidence_source="health_cache",
            credentials_present=_credentials(provider),
            credential_overrides=tuple(
                (str(row["id"]), _credentials(row))
                for row in provider.get("models", [])
                if isinstance(row, Mapping) and ("apiKey" in row or "headers" in row)
            ),
            project_scope_override=override,
            path_recognized=report.config_path == self.paths.models,
        )
        return PrimeDependencies(registry.raw_bytes, context, project_digest)


def _credentials(config: Mapping[str, Any]) -> bool | None:
    """Inspect presence only; arbitrary shell helpers/headers stay unknown."""
    if "headers" in config:
        return None
    value = config.get("apiKey")
    if not isinstance(value, str) or not value:
        return False
    if value.startswith("!"):
        return None
    if re.fullmatch(r"[A-Z_][A-Z0-9_]*", value):
        return bool(os.environ.get(value))
    return True


def prime_reader(gateway: str) -> PrimeReadAdapter:
    # Do not resolve away symlinks: the transaction loader must reject them.
    agent_dir = default_prime_agent_dir().absolute()
    return PrimeReadAdapter(SelectionPaths(agent_dir, Path.cwd() / ".prime/settings.json"), gateway)


def _result(value: Any) -> ActionResult:
    data = value.to_dict()
    ok = not data.get("refusals") and data.get("status") not in {"refused", "busy"}
    return ActionResult(data=data, ok=ok, exit_code=0 if ok else 2)


def _refusal() -> ActionResult:
    return ActionResult(
        data={"error": "bootstrap refused: unsafe or missing local input"}, ok=False, exit_code=2
    )


def action_prime_select_preview(**kwargs: Any) -> ActionResult:
    try:
        preview = preview_selection(
            kwargs["settings"],
            kwargs["projection_rows"],
            selected_ids=kwargs["selected_ids"],
            dependencies=kwargs["dependencies"],
            now=kwargs.get("now") or utc_now(),
        )
        return _result(preview)
    except (KeyError, ValueError, OSError, TypeError):
        return _refusal()


def action_prime_select_apply(**kwargs: Any) -> ActionResult:
    try:
        return _result(
            apply_selection(
                kwargs["preview"],
                expected_pre_digest=kwargs["expected_pre_digest"],
                confirmed=kwargs.get("confirmed") is True,
                paths=kwargs["paths"],
                reload_rows=kwargs["reload_rows"],
                reload_dependencies=kwargs["reload_dependencies"],
                clock=kwargs.get("clock") or utc_now,
            )
        )
    except (KeyError, ValueError, OSError, TypeError):
        return _refusal()


def action_prime_restore_preview(**kwargs: Any) -> ActionResult:
    try:
        return _result(
            preview_restore(
                kwargs["settings"], kwargs["receipt"], backup_bytes=kwargs["backup_bytes"]
            )
        )
    except (KeyError, ValueError, OSError, TypeError):
        return _refusal()


def action_prime_restore_apply(**kwargs: Any) -> ActionResult:
    try:
        return _result(
            restore_selection(
                kwargs["preview"],
                expected_post_digest=kwargs["expected_post_digest"],
                confirmed=kwargs.get("confirmed") is True,
                paths=kwargs["paths"],
                reload_dependencies=kwargs["reload_dependencies"],
                clock=kwargs.get("clock") or utc_now,
            )
        )
    except (KeyError, ValueError, OSError, TypeError):
        return _refusal()


def restore_preview(reader: PrimeReadAdapter, transaction_id: str | None = None) -> Any:
    dependencies = reader.dependencies()
    if not dependencies.discovery.installed:
        raise PrimeSelectionError("binary_missing")
    _, receipt, backup = load_receipt(reader.paths, transaction_id=transaction_id)
    return preview_restore(load_settings(reader.paths.settings), receipt, backup_bytes=backup)


def action_claude_compat(**kwargs: Any) -> ActionResult:
    from verdict.harness_claude import discover, resolve_paths, status
    from verdict.harness_claude_compat import build_claude_compat_report
    from verdict.tui_completion_snapshot import load_snapshot

    try:
        mode = kwargs.get("mode", "native")
        if mode not in {"native", "openai-side-path"}:
            return _refusal()
        selected: Sequence[str] = kwargs.get("selected_ids") or ()
        source = "explicit exact ids"
        rows = kwargs.get("projection_rows")
        if rows is None:
            rows = load_snapshot(
                kwargs.get("snapshot_path"), now=kwargs.get("now") or utc_now()
            ).model_rows
        if not selected:
            source = "current exact Prime scope joined to last local projection"
            try:
                scope = load_settings(
                    (
                        kwargs.get("prime_paths") or prime_reader("http://127.0.0.1:20128").paths
                    ).settings
                ).value.get("enabledModels", [])
                known = {row["route_id"] for row in rows}
                selected = [rid for rid in scope if exact_token(rid) and rid in known]
            except (OSError, ValueError):
                selected = ()
        if any(not exact_token(rid) or rid in {"apply", "select", "restore"} for rid in selected):
            return _refusal()
        path = resolve_paths(claude_home=kwargs.get("claude_home")).config

        def digest() -> str | None:
            try:
                return byte_digest(load_settings(path).raw_bytes)
            except (ValueError, OSError):
                return None

        start = digest()
        discovery = kwargs.get("discovery") or discover(claude_home=kwargs.get("claude_home"))
        report_status = kwargs.get("status") or status(claude_home=kwargs.get("claude_home"))
        end = digest()
        report = build_claude_compat_report(
            discovery,
            report_status,
            kwargs.get("start_digest", start),
            kwargs.get("end_digest", end),
            mode,
            selected,
            rows,
            kwargs.get("now") or utc_now(),
        )
        report["selected_ids_source"] = source
        return ActionResult(data=report)
    except (ValueError, OSError, TypeError, KeyError):
        return _refusal()
