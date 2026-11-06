"""Host prerequisites ``verdict setup`` installs and ``verdict doctor`` reports (BOD-219).

These six were done by hand on the operator host on 2026-11-05:

* ``host.ocr``: the open-code-review release pinned in
  ``.github/workflows/certify-rehearsal.yml``, SHA-256 verified, at ``~/.local/bin/ocr``;
* ``host.metadata``: ``verdict metadata refresh`` when the Core metadata store is
  missing, then the six-hour refresh timer;
* ``host.acceptance-timer`` and ``host.identify-timers``: the hourly acceptance timer
  and the passive and screen identify timers;
* ``host.serve``: the ``verdict serve`` user unit and its private env file;
* ``host.prime-registry``: a missing ``~/.prime/agent/models.json`` gets
  ``providers.omniroute``, then ``harness prime sync-models`` and ``enable``.

Planning (``plan_host_actions``) only observes. Each unsatisfied prerequisite
becomes one consent-required capability-bootstrap action, so APPLY is gated by
the same ``--yes`` / ``--allow`` policy as every other setup action
(``capability_bootstrap.authorize_bootstrap_actions``). ``run_host_action`` then
calls the existing installers; nothing here re-implements one.
"""

from __future__ import annotations

import os
import shutil
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from verdict import ocr_release
from verdict.capability_bootstrap import HOST_PREREQUISITE_KIND, BootstrapAction

HOST_ACTION_KIND = HOST_PREREQUISITE_KIND
OCR = "host.ocr"
METADATA = "host.metadata"
ACCEPTANCE = "host.acceptance-timer"
IDENTIFY = "host.identify-timers"
SERVE = "host.serve"
PRIME = "host.prime-registry"
#: Apply order: Prime enable needs a healthy serve, so the serve unit comes first.
ORDER = (OCR, METADATA, ACCEPTANCE, IDENTIFY, SERVE, PRIME)
#: ``harness prime enable`` waits this many probes (one second apart) for serve.
SERVE_HEALTH_ATTEMPTS = 15
PRIME_SYNC_UNIT = "verdict-prime-sync.service"


def setup_repair(provider_id: str) -> str:
    """The exact command that installs one prerequisite."""
    return f"verdict setup --apply --allow {provider_id}"


def _unit_state(unit: str) -> Mapping[str, Any]:
    from verdict import prove_at_rest_service

    return prove_at_rest_service.timer_state(unit)


def _unit_result(unit: str) -> str | None:
    from verdict.metadata_service import _run_show, _show

    values = _show(unit, _run_show)
    if values.get("LoadState") != "loaded":
        return None
    return values.get("Result") or None


def _systemctl(args: list[str]) -> None:
    from verdict.prove_at_rest_service import _systemctl as run

    run(args)


def _metadata_refresh() -> Mapping[str, Any]:
    from verdict.actions.registry import run_action

    result = run_action("metadata.refresh", {"include_p1": True})
    return {"ok": result.ok, **result.data}


def _prime_sync(gateway: str) -> Mapping[str, Any]:
    from verdict.actions.registry import run_action

    result = run_action("harness.prime.sync-models", {"gateway": gateway})
    data = result.data
    return {
        "ok": result.ok,
        "error": data.get("error"),
        "total": data.get("total"),
        "added": list(data.get("added") or ()),
        "removed": list(data.get("removed") or ()),
    }


def _serve_health() -> bool:
    from verdict.serve_service import healthy

    return healthy()


@dataclass(frozen=True)
class HostEnvironment:
    """Where setup and doctor look, and the side-effecting steps they may take."""

    home: Path
    unit_dir: Path
    repo_root: Path
    gateway: str
    prime_home: Path | None = None
    which: Callable[[str], str | None] = shutil.which
    ocr_version: Callable[[Path], str | None] = ocr_release.probe_version
    unit_state: Callable[[str], Mapping[str, Any]] = _unit_state
    unit_result: Callable[[str], str | None] = _unit_result
    systemctl: Callable[[list[str]], None] = _systemctl
    fetch: Callable[[str], bytes] = ocr_release.fetch_release_asset
    metadata_refresh: Callable[[], Mapping[str, Any]] = _metadata_refresh
    prime_sync: Callable[[str], Mapping[str, Any]] = _prime_sync
    serve_health: Callable[[], bool] = _serve_health
    sleep: Callable[[float], None] = field(default=time.sleep)

    @classmethod
    def from_host(cls) -> HostEnvironment:
        from verdict.credentials_store import resolve_gateway_binding

        home = Path.home()
        config = Path(os.environ.get("XDG_CONFIG_HOME", str(home / ".config")))
        gateway = resolve_gateway_binding()[0].removesuffix("/v1")
        return cls(
            home=home,
            unit_dir=config / "systemd" / "user",
            repo_root=ocr_release.package_root(),
            gateway=gateway,
        )


# --- observation (read-only) ---------------------------------------------------------


def _ocr_location(env: HostEnvironment) -> Path | None:
    located = env.which("ocr")
    if located:
        return Path(located)
    target = ocr_release.install_target(env.home)
    return target if target.is_file() else None


def _inspect_ocr(env: HostEnvironment) -> ocr_release.OcrInspection:
    version, digest = ocr_release.ocr_pin(env.repo_root)
    return ocr_release.inspect_ocr(
        _ocr_location(env), version=version, digest=digest, reported_version=env.ocr_version
    )


def _inactive(env: HostEnvironment, *units: str) -> list[str]:
    return [
        f"{unit} is {env.unit_state(unit).get('state', 'unknown')}"
        for unit in units
        if env.unit_state(unit).get("state") != "active"
    ]


def _timer_problems(env: HostEnvironment, rendered: Mapping[str, Any], *units: str) -> list[str]:
    problems = []
    if rendered.get("changed"):
        names = ", ".join(sorted(rendered.get("files") or ()))
        problems.append(f"unit files missing or not the rendered ones ({names})")
    return problems + _inactive(env, *units)


def _observe(env: HostEnvironment, provider_id: str) -> list[str]:
    """What is wrong with one prerequisite; empty when it is satisfied."""
    if provider_id == OCR:
        try:
            found = _inspect_ocr(env)
        except (OSError, ValueError):
            return []  # no pin to install from: doctor reports it, setup cannot act
        return [] if found.ok else [found.describe()]
    if provider_id == METADATA:
        from verdict import metadata_service
        from verdict.metadata import default_store_path

        problems = [] if default_store_path().is_file() else [f"{default_store_path()} is missing"]
        rendered = metadata_service.manage_service(unit_dir=env.unit_dir, dry_run=True)
        return problems + _timer_problems(env, rendered, metadata_service.TIMER)
    if provider_id == ACCEPTANCE:
        from verdict import acceptance_service

        rendered = acceptance_service.manage_service(unit_dir=env.unit_dir, dry_run=True)
        return _timer_problems(env, rendered, acceptance_service.TIMER)
    if provider_id == IDENTIFY:
        from verdict import identify_service

        rendered = identify_service.manage_service(unit_dir=env.unit_dir, dry_run=True)
        return _timer_problems(env, rendered, identify_service.TIMER, identify_service.SCREEN_TIMER)
    if provider_id == SERVE:
        from verdict import serve_service

        return serve_service.install_problems(unit_dir=env.unit_dir, home=env.home) + _inactive(
            env, serve_service.SERVICE
        )
    from verdict.harness_prime import resolve_paths

    models = resolve_paths(prime_home=env.prime_home).models
    return [] if models.exists() else [f"{models} is missing"]


_ACTIONS: dict[str, dict[str, str]] = {
    OCR: {
        "description": "Install the pinned open-code-review (ocr) release at ~/.local/bin/ocr.",
        "security_impact": (
            "Downloads one GitHub release asset over HTTPS and installs it only when its "
            "SHA-256 equals the certify-rehearsal.yml pin; a mismatch installs nothing."
        ),
        "postcondition": "~/.local/bin/ocr hashes to OCR_SHA256 from certify-rehearsal.yml.",
        "undo": (
            "Remove ~/.local/bin/ocr; restore ~/.local/bin/ocr.verdict.bak if setup "
            "replaced a binary."
        ),
        "install_command": "download the pinned asset, verify sha256, install ~/.local/bin/ocr",
    },
    METADATA: {
        "description": (
            "Refresh the Core metadata store if it is missing, then install the six-hour "
            "metadata refresh timer."
        ),
        "security_impact": (
            "Fetches public model metadata catalogs; writes ~/.verdict/model-metadata.json "
            "and two systemd user units. No credentials are read."
        ),
        "postcondition": "The store exists and verdict-metadata-refresh.timer is active.",
        "undo": "verdict metadata install-service --uninstall",
        "install_command": "verdict metadata refresh --include-p1; verdict metadata install-service",
    },
    ACCEPTANCE: {
        "description": "Install the hourly live acceptance timer.",
        "security_impact": "Writes two systemd user units; notifications stay dry-run.",
        "postcondition": "verdict-acceptance.timer is active.",
        "undo": "verdict acceptance install-service --uninstall",
        "install_command": "verdict acceptance install-service",
    },
    IDENTIFY: {
        "description": "Install the passive identify and bounded screen identify timers.",
        "security_impact": (
            "Writes four systemd user units; the daily screen runs bounded live probes "
            "(free and subscription capacity only)."
        ),
        "postcondition": "verdict-identify.timer and verdict-identify-screen.timer are active.",
        "undo": "verdict identify uninstall-service",
        "install_command": "verdict identify install-service",
    },
    SERVE: {
        "description": (
            "Install the verdict serve user unit (127.0.0.1:8000) and its private env file."
        ),
        "security_impact": (
            "Writes ~/.config/verdict/serve.env (0600) with LLMGATE_AUTH_TOKEN and "
            "VERDICT_RECEIPTS_DB; reuses an existing token or generates one and stores it "
            "in the credential store. Values are never printed."
        ),
        "postcondition": "verdict-serve.service is active and answers /health.",
        "undo": (
            "systemctl --user disable --now verdict-serve.service; remove "
            "~/.config/systemd/user/verdict-serve.service and ~/.config/verdict/serve.env "
            "(replaced files are copied under $VERDICT_HOME/service-rollback/)"
        ),
        "install_command": "render verdict-serve.service; systemctl --user enable --now",
    },
    PRIME: {
        "description": (
            "Create ~/.prime/agent/models.json with providers.omniroute, then sync its "
            "models and enable the Verdict Prime harness."
        ),
        "security_impact": (
            "Writes models.json (0600); apiKey is the env var name OMNIROUTE_API_KEY, never "
            "a value. Reads the gateway /v1/models inventory."
        ),
        "postcondition": "providers.omniroute lists the live models; providers.verdict is set.",
        "undo": "verdict harness prime disable, then remove ~/.prime/agent/models.json",
        "install_command": "verdict harness prime sync-models; verdict harness prime enable",
    },
}


def _action(provider_id: str, problems: Sequence[str]) -> BootstrapAction:
    text = _ACTIONS[provider_id]
    return BootstrapAction(
        action_id=f"bootstrap:host:{provider_id.removeprefix('host.')}",
        kind=HOST_ACTION_KIND,
        target=provider_id,
        description=text["description"],
        reason="; ".join(problems),
        security_impact=text["security_impact"],
        postcondition=text["postcondition"],
        undo=text["undo"],
        reversible=True,
        requires_consent=True,
        provider_id=provider_id,
        install_command=text["install_command"],
        install_source="verdict",
        privilege="user",
    )


def plan_host_actions(env: HostEnvironment) -> tuple[BootstrapAction, ...]:
    """One consent-required action per unsatisfied prerequisite, in apply order. Read-only."""
    actions = []
    for provider_id in ORDER:
        problems = _observe(env, provider_id)
        if problems:
            actions.append(_action(provider_id, problems))
    return tuple(actions)


# --- apply (only after the bootstrap consent gate) ---------------------------------------


def _failed(note: str, **extra: Any) -> dict[str, Any]:
    return {"status": "failed", "note": note, **extra}


def _install_ocr(env: HostEnvironment) -> dict[str, Any]:
    version, digest = ocr_release.ocr_pin(env.repo_root)
    result = ocr_release.install_release(
        version=version, digest=digest, target=ocr_release.install_target(env.home), fetch=env.fetch
    )
    if result["status"] != "installed":
        return _failed(str(result["note"]), ocr=result)
    return {"status": "applied", "note": result["note"], "ocr": result}


def _install_timers(env: HostEnvironment, manage: Callable[..., dict[str, Any]]) -> dict[str, Any]:
    result = manage(unit_dir=env.unit_dir, run=env.systemctl)
    return {"plan": result.get("plan"), "changed": result.get("changed")}


def _install_metadata(env: HostEnvironment) -> dict[str, Any]:
    from verdict import metadata_service
    from verdict.metadata import default_store_path

    steps: dict[str, Any] = {}
    refresh_note = ""
    if not default_store_path().is_file():
        refreshed = env.metadata_refresh()
        steps["refresh"] = {"ok": bool(refreshed.get("ok")), "error": refreshed.get("error")}
        if not refreshed.get("ok"):
            refresh_note = (
                f"metadata refresh failed ({refreshed.get('error') or 'no detail'}); "
                "repair: verdict metadata refresh"
            )
    steps["timer"] = _install_timers(env, metadata_service.manage_service)
    if env.unit_state(metadata_service.TIMER).get("state") != "active":
        # The installer enables only when unit files change; an unchanged,
        # disabled install still needs this one step.
        env.systemctl(["systemctl", "--user", "enable", "--now", metadata_service.TIMER])
    if refresh_note:
        return _failed(f"{refresh_note}; the timer was installed and retries", **steps)
    return {"status": "applied", "note": f"{metadata_service.TIMER} installed", **steps}


def _install_serve(env: HostEnvironment) -> dict[str, Any]:
    from verdict import serve_service

    result = serve_service.manage_service(unit_dir=env.unit_dir, home=env.home, run=env.systemctl)
    keep = ("plan", "env_file", "env_file_plan", "env_keys", "token_source", "token_stored")
    return {
        "status": "applied",
        "note": f"{serve_service.SERVICE} enabled; token source {result['token_source']}",
        **{key: result[key] for key in keep},
    }


def _wait_for_serve(env: HostEnvironment) -> bool:
    for attempt in range(SERVE_HEALTH_ATTEMPTS):
        if env.serve_health():
            return True
        if attempt + 1 < SERVE_HEALTH_ATTEMPTS:
            env.sleep(1.0)
    return False


def _install_prime_registry(env: HostEnvironment) -> dict[str, Any]:
    from verdict import harness_prime

    path = harness_prime.create_omniroute_registry(gateway=env.gateway, prime_home=env.prime_home)
    steps: dict[str, Any] = {"registry": str(path)}
    synced = env.prime_sync(env.gateway)
    steps["sync"] = {key: synced.get(key) for key in ("ok", "total", "added", "removed")}
    if not synced.get("ok"):
        return _failed(
            f"created {path}; sync-models failed ({synced.get('error') or 'no detail'}); "
            "repair: verdict harness prime sync-models, then verdict harness prime enable",
            **steps,
        )
    try:
        enabled = harness_prime.enable(
            prime_home=env.prime_home, health_check=lambda base_url: _wait_for_serve(env)
        )
    except harness_prime.HarnessPrimeError as exc:
        return _failed(
            f"created {path} and synced its models; enable failed ({exc}); start verdict "
            f"serve ({setup_repair(SERVE)}), then repair: verdict harness prime enable",
            **steps,
        )
    steps["enable"] = {"base_url": enabled.base_url, "token_env": enabled.token_env}
    return {"status": "applied", "note": f"created and synced {path}; Prime enabled", **steps}


def run_host_action(action: BootstrapAction, env: HostEnvironment) -> Mapping[str, Any]:
    """Apply one authorized host action with the existing installer. Never raises."""
    from verdict import acceptance_service, identify_service

    provider_id = action.provider_id or ""
    try:
        if provider_id == OCR:
            return _install_ocr(env)
        if provider_id == METADATA:
            return _install_metadata(env)
        if provider_id == ACCEPTANCE:
            steps = _install_timers(env, acceptance_service.manage_service)
            return {"status": "applied", "note": f"{acceptance_service.TIMER} enabled", **steps}
        if provider_id == IDENTIFY:
            steps = _install_timers(env, identify_service.manage_service)
            return {"status": "applied", "note": "identify timers enabled", **steps}
        if provider_id == SERVE:
            return _install_serve(env)
        if provider_id == PRIME:
            return _install_prime_registry(env)
    except Exception as exc:  # report, never crash the bootstrap report
        return _failed(f"{type(exc).__name__}: {exc}")
    return {"status": "blocked", "note": f"unknown host prerequisite {provider_id!r}"}


# --- doctor rows (read-only, same observations as setup) ----------------------------------


def _row(label: str, status: str, detail: str) -> dict[str, str]:
    return {"label": label, "status": status, "detail": detail}


def _ocr_row(env: HostEnvironment) -> dict[str, str]:
    label = "Review tool (ocr)"
    try:
        found = _inspect_ocr(env)
    except (OSError, ValueError) as exc:
        return _row(label, "warning", f"cannot verify ocr: {exc}")
    if not found.ok:
        return _row(label, "failed", f"{found.describe()}; repair: {setup_repair(OCR)}")
    if found.path is not None and env.which("ocr") is None:
        directory = found.path.parent
        return _row(
            label,
            "failed",
            f"{found.describe()}, but {directory} is not on PATH; "
            f'repair: export PATH="{directory}:$PATH"',
        )
    return _row(label, "ok", found.describe())


def _serve_rows(env: HostEnvironment) -> list[dict[str, str]]:
    from verdict.serve_service import HEALTH_URL, SERVICE

    label = "Verdict serve unit"
    logs = f"logs: journalctl --user -u {SERVICE}"
    state = str(env.unit_state(SERVICE).get("state", "unknown"))
    if state == "missing":
        return [_row(label, "failed", f"{SERVICE} is not installed; repair: {setup_repair(SERVE)}")]
    if state != "active":
        return [
            _row(
                label,
                "failed",
                f"{SERVICE} is {state}; repair: systemctl --user enable --now {SERVICE}; {logs}",
            )
        ]
    if env.serve_health():
        health = _row("Verdict serve health", "ok", f"{HEALTH_URL} answers")
    else:
        health = _row(
            "Verdict serve health",
            "failed",
            f"{HEALTH_URL} did not answer; repair: systemctl --user restart {SERVICE}; {logs}",
        )
    return [_row(label, "ok", f"{SERVICE} is active"), health]


def _prime_rows(env: HostEnvironment) -> list[dict[str, str]]:
    from verdict.harness_prime import omniroute_registry_state

    label = "Prime registry"
    registry = omniroute_registry_state(prime_home=env.prime_home)
    path, state = registry["path"], registry["state"]
    sync = "repair: verdict harness prime sync-models"
    if state == "missing":
        row = _row(label, "failed", f"{path} is missing; repair: {setup_repair(PRIME)}")
    elif state == "unreadable":
        row = _row(
            label,
            "failed",
            f"{path} is unreadable ({registry.get('error')}); move it aside, then "
            f"repair: {setup_repair(PRIME)}",
        )
    elif state == "no_provider":
        row = _row(
            label,
            "failed",
            f"{path} has no providers.omniroute entry; add one (baseUrl {env.gateway}/v1, "
            f"api openai-completions, apiKey OMNIROUTE_API_KEY), then {sync}",
        )
    elif state == "no_models":
        row = _row(label, "failed", f"{path} providers.omniroute has no models; {sync}")
    else:
        row = _row(label, "ok", f"{path}: providers.omniroute has {registry['models']} model(s)")
    rows = [row]
    result = env.unit_result(PRIME_SYNC_UNIT)
    if result is not None:
        detail = f"{PRIME_SYNC_UNIT} last result {result}"
        if result != "success":
            detail += f"; {sync}; logs: journalctl --user -u {PRIME_SYNC_UNIT}"
        rows.append(_row("Prime-sync service", "ok" if result == "success" else "failed", detail))
    return rows


def doctor_rows(env: HostEnvironment) -> list[dict[str, str]]:
    """``verdict doctor`` rows for ocr, the serve unit and the Prime registry.

    The timers have rows in ``doctor_diagnostics.installed_service_rows``. Every
    problem row ends with the exact repair command (``repair: ...``).
    """
    return [_ocr_row(env), *_serve_rows(env), *_prime_rows(env)]


def bootstrap_options(env: HostEnvironment | None = None) -> dict[str, Any]:
    """``run_bootstrap`` keyword arguments that add the host prerequisites."""
    resolved = env or HostEnvironment.from_host()

    def planner() -> tuple[BootstrapAction, ...]:
        return plan_host_actions(resolved)

    def runner(action: BootstrapAction) -> Mapping[str, Any]:
        return run_host_action(action, resolved)

    return {"host_planner": planner, "host_runner": runner}
