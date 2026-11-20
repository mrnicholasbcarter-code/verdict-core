"""Host prerequisites ``verdict setup`` installs and ``verdict doctor`` reports (BOD-219).

These six were done by hand on the operator host on 2026-11-05:

* ``host.ocr``: the open-code-review release pinned in ``verdict.ocr_release``,
  SHA-256 verified, at ``~/.local/bin/ocr``;
* ``host.metadata``: ``verdict metadata refresh`` when the Core metadata store is
  missing, then the six-hour refresh timer;
* ``host.acceptance-timer`` and ``host.identify-timers``: the hourly acceptance timer
  and the passive and screen identify timers;
* ``host.serve``: the ``verdict serve`` user unit and its private env file;
* ``host.prime-registry``: a missing ``~/.prime/agent/models.json`` gets
  ``providers.omniroute``, then ``harness prime sync-models`` and ``enable``.

Added later:

* ``host.ref-docs-timer``: the hourly ``verdict memory ref-docs sync`` timer.
* ``host.docs-refresh-timer``: the six-hourly ``verdict memory docs --fix`` timer that keeps
  the authoritative project documentation fresh for ``verdict doctor``.

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
REF_DOCS = "host.ref-docs-timer"
DOCS_REFRESH = "host.docs-refresh-timer"
PR_WATCHER = "host.pr-watcher-timer"
CONTEXT_LIFT = "host.context-lift-timer"
#: Apply order: Prime enable needs a healthy serve, so the serve unit comes first.
ORDER = (
    OCR,
    METADATA,
    ACCEPTANCE,
    IDENTIFY,
    REF_DOCS,
    DOCS_REFRESH,
    PR_WATCHER,
    CONTEXT_LIFT,
    SERVE,
    PRIME,
)
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


def _docs_ready() -> bool:
    """Read-only documentation preflight for the checkout the refresh service uses."""
    from verdict.documentation_preflight import run_documentation_preflight

    root = Path(__file__).resolve().parent.parent
    return bool(run_documentation_preflight(repo_root=root, fix=False).passed)


def docs_refresh_command() -> str:
    """The command that refreshes the documentation index (what the service runs)."""
    return f"verdict memory docs --fix --repo-root {Path(__file__).resolve().parent.parent}"


@dataclass(frozen=True)
class HostEnvironment:
    """Where setup and doctor look, and the side-effecting steps they may take."""

    home: Path
    unit_dir: Path
    gateway: str
    prime_home: Path | None = None
    which: Callable[[str], str | None] = shutil.which
    ocr_pin: Callable[[], tuple[str, str]] = ocr_release.ocr_pin
    ocr_version: Callable[[Path], str | None] = ocr_release.probe_version
    unit_state: Callable[[str], Mapping[str, Any]] = _unit_state
    unit_result: Callable[[str], str | None] = _unit_result
    systemctl: Callable[[list[str]], None] = _systemctl
    fetch: Callable[[str], bytes] = ocr_release.fetch_release_asset
    metadata_refresh: Callable[[], Mapping[str, Any]] = _metadata_refresh
    prime_sync: Callable[[str], Mapping[str, Any]] = _prime_sync
    serve_health: Callable[[], bool] = _serve_health
    docs_ready: Callable[[], bool] = _docs_ready
    sleep: Callable[[float], None] = field(default=time.sleep)

    @classmethod
    def from_host(cls) -> HostEnvironment:
        from verdict.credentials_store import resolve_gateway_binding

        home = Path.home()
        config = Path(os.environ.get("XDG_CONFIG_HOME", str(home / ".config")))
        gateway = resolve_gateway_binding()[0].removesuffix("/v1")
        return cls(home=home, unit_dir=config / "systemd" / "user", gateway=gateway)


# --- observation (read-only) ---------------------------------------------------------


def _ocr_location(env: HostEnvironment) -> Path | None:
    located = env.which("ocr")
    if located:
        return Path(located)
    target = ocr_release.install_target(env.home)
    return target if target.is_file() else None


def _inspect_ocr(env: HostEnvironment) -> ocr_release.OcrInspection:
    version, digest = env.ocr_pin()
    return ocr_release.inspect_ocr(
        _ocr_location(env), version=version, digest=digest, reported_version=env.ocr_version
    )


def _probe_serve(env: HostEnvironment) -> tuple[bool, str]:
    """``(healthy, reason)``; a probe that raises is unhealthy, never unknown-as-ok."""
    try:
        return bool(env.serve_health()), ""
    except Exception as exc:
        return False, f" ({type(exc).__name__})"


def _inactive(env: HostEnvironment, *units: str) -> list[str]:
    return [
        f"{unit} is {env.unit_state(unit).get('state', 'unknown')}"
        for unit in units
        if env.unit_state(unit).get("state") != "active"
    ]


def _docs_index_ready(env: HostEnvironment) -> bool:
    """A preflight that cannot run is "not ready": the refresh is the repair either way."""
    try:
        return bool(env.docs_ready())
    except Exception:
        return False


def _timer_problems(env: HostEnvironment, rendered: Mapping[str, Any], *units: str) -> list[str]:
    problems = []
    if rendered.get("changed"):
        names = ", ".join(sorted(rendered.get("files") or ()))
        problems.append(f"unit files missing or not the rendered ones ({names})")
    return problems + _inactive(env, *units)


def _pr_watch_config_path() -> Path:
    return (
        Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
        / "verdict"
        / "pr-watch.env"
    )


def _pr_watch_repos() -> list[str]:
    """Read and validate repositories from pr-watch.env.

    Returns empty list if file missing. Raises ValueError with reason if file
    exists but is invalid (permissions, format, or invalid repo names).
    """
    import re
    import stat

    path = _pr_watch_config_path()
    try:
        st = path.stat()
    except OSError:
        return []

    # Check file permissions: must not be group/world-writable
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ValueError(
            f"pr-watch.env has unsafe permissions (mode {oct(st.st_mode & 0o777)}): "
            "must not be group or world writable"
        )
    # Check ownership: must be owned by current user
    if st.st_uid != os.getuid():
        raise ValueError(
            f"pr-watch.env is owned by uid {st.st_uid}, not current user {os.getuid()}"
        )

    try:
        content = path.read_text()
    except OSError:
        return []

    lines = content.splitlines()
    raw_value = ""
    for line in lines:
        # Strip the line, remove export prefix, remove trailing comment
        stripped = line.strip()
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :].strip()
        if " #" in stripped:
            stripped = stripped.split(" #", 1)[0].rstrip()
        if stripped.startswith("VERDICT_PR_WATCH_REPOS="):
            raw_value = stripped.split("=", 1)[1]
            break

    # Strip surrounding quotes, leading/trailing spaces
    value = raw_value.strip()
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        value = value[1:-1]

    repos = [repo.strip() for repo in value.split(",") if repo.strip()]

    # Validate each repo: ^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$ without ..
    repo_pattern = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
    for repo in repos:
        if ".." in repo or not repo_pattern.match(repo):
            raise ValueError(f"invalid repository name in pr-watch.env: {repo!r}")

    return repos


def _pr_watch_problem() -> str:
    return f"no repositories configured: write VERDICT_PR_WATCH_REPOS to {_pr_watch_config_path()}"


#: Doctor row state for an optional part that is absent: never ok, never an issue.
NOT_INSTALLED = "not-installed"
PRIME_OPTIONAL_DETAIL = (
    "Prime Agent not installed (optional harness); the default worker executor is "
    "direct-gateway. To use Prime, install prime-agent, run verdict setup, then "
    "pass --executor prime-headless"
)


def prime_installed(env: HostEnvironment) -> bool:
    """The Prime Agent binary is on this host's PATH (``prime-agent`` or ``prime``)."""
    from verdict.harness_prime import binary_installed

    return binary_installed(env.which)


def _observe(env: HostEnvironment, provider_id: str) -> list[str]:
    """What is wrong with one prerequisite; empty when it is satisfied."""
    if provider_id == OCR:
        found = _inspect_ocr(env)
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
    if provider_id == REF_DOCS:
        from verdict import ref_docs_service

        rendered = ref_docs_service.manage_service(unit_dir=env.unit_dir, dry_run=True)
        return _timer_problems(env, rendered, ref_docs_service.TIMER)
    if provider_id == DOCS_REFRESH:
        from verdict import docs_refresh_service

        rendered = docs_refresh_service.manage_service(unit_dir=env.unit_dir, dry_run=True)
        problems = _timer_problems(env, rendered, docs_refresh_service.TIMER)
        if not problems and not _docs_index_ready(env):
            # An install changes the checkout commit the index is keyed to; the
            # timer would leave it stale for up to six hours.
            problems.append("documentation index is not ready")
        return problems
    if provider_id == PR_WATCHER:
        from verdict import pr_watcher_service

        try:
            repos = _pr_watch_repos()
        except ValueError as exc:
            return [str(exc)]
        if not repos:
            return [_pr_watch_problem()]
        rendered = pr_watcher_service.manage_service(
            unit_dir=env.unit_dir, dry_run=True, repos=repos
        )
        return _timer_problems(env, rendered, pr_watcher_service.TIMER)
    if provider_id == CONTEXT_LIFT:
        from verdict import context_lift_service

        rendered = context_lift_service.manage_service(unit_dir=env.unit_dir, dry_run=True)
        return _timer_problems(env, rendered, context_lift_service.TIMER)
    if provider_id == SERVE:
        from verdict import serve_service

        problems = serve_service.install_problems(unit_dir=env.unit_dir, home=env.home)
        inactive = _inactive(env, serve_service.SERVICE)
        if not inactive:
            # Active is not healthy: an unanswered or failed probe plans the restart.
            healthy, why = _probe_serve(env)
            if not healthy:
                problems.append(
                    f"{serve_service.HEALTH_URL} did not answer{why}; "
                    f"setup restarts {serve_service.SERVICE}"
                )
        return problems + inactive
    from verdict.harness_prime import resolve_paths

    if not prime_installed(env):
        # Prime Agent is an optional harness: nothing to set up without it.
        return []
    models = resolve_paths(prime_home=env.prime_home).models
    return [] if models.exists() else [f"{models} is missing"]


_ACTIONS: dict[str, dict[str, str]] = {
    OCR: {
        "description": "Install the pinned open-code-review (ocr) release at ~/.local/bin/ocr.",
        "security_impact": (
            "Downloads one GitHub release asset over HTTPS and installs it only when its "
            "SHA-256 equals the verdict.ocr_release pin; a mismatch installs nothing."
        ),
        "postcondition": "~/.local/bin/ocr hashes to OCR_SHA256 in verdict.ocr_release.",
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
            "Writes four systemd user units; the six-hourly screen runs bounded live probes "
            "(free and subscription capacity only)."
        ),
        "postcondition": "verdict-identify.timer and verdict-identify-screen.timer are active.",
        "undo": "verdict identify uninstall-service",
        "install_command": "verdict identify install-service",
    },
    REF_DOCS: {
        "description": "Install the hourly reference-docs sync timer.",
        "security_impact": (
            "Writes two systemd user units; the hourly job reads public GitHub docs via gh "
            "and writes pages to the local ai-memory project ref-omniroute."
        ),
        "postcondition": "verdict-ref-docs.timer is active.",
        "undo": "verdict memory ref-docs install-service --uninstall",
        "install_command": "verdict memory ref-docs install-service",
    },
    DOCS_REFRESH: {
        "description": "Install the six-hourly documentation refresh timer.",
        "security_impact": (
            "Writes two systemd user units; every six hours it runs verdict memory docs --fix "
            "on this checkout and writes only the local shared memory database. No model calls."
        ),
        "postcondition": "verdict-docs-refresh.timer is active.",
        "undo": "verdict memory docs --install-service --uninstall",
        "install_command": "verdict memory docs --install-service",
    },
    PR_WATCHER: {
        "description": (
            "Install the 5-minute PR watcher timer that polls repositories, classifies CI, "
            "and emits bounded reruns."
        ),
        "security_impact": (
            "Reads GitHub through gh api graphql; its only write is a bounded rerun of a failed "
            "required CI job (POST /repos/{owner}/{repo}/actions/jobs/{id}/rerun), fenced on the "
            "exact head; writes local state under the git common dir."
        ),
        "postcondition": "verdict-pr-watch.timer is active.",
        "undo": "systemctl --user disable --now verdict-pr-watch.timer; remove verdict-pr-watch.service and verdict-pr-watch.timer",
        "install_command": "verdict setup --apply --allow host.pr-watcher-timer",
    },
    CONTEXT_LIFT: {
        "description": "Install the daily context-lift bench timer (free capacity only).",
        "security_impact": (
            "Writes two systemd user units; the daily job runs "
            "verdict bench context-lift --json on FREE capacity only, "
            "making at most a handful of live model calls, and writes evidence "
            "under $VERDICT_HOME/context-lift/. No paid routes are used."
        ),
        "postcondition": "verdict-context-lift.timer is active.",
        "undo": "verdict bench context-lift --install-service --uninstall",
        "install_command": "verdict setup --apply --allow host.context-lift-timer",
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
    version, digest = env.ocr_pin()
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

    service = serve_service.SERVICE
    result = serve_service.manage_service(unit_dir=env.unit_dir, home=env.home, run=env.systemctl)
    keep = ("plan", "env_file", "env_file_plan", "env_keys", "token_source", "token_stored")
    facts = {key: result[key] for key in keep}
    restarted = False
    if not result["changed"] and not _probe_serve(env)[0]:
        # Unchanged files mean the installer started nothing new: restart it.
        env.systemctl(["systemctl", "--user", "restart", service])
        restarted = True
    if not _wait_for_serve(env):
        return _failed(
            f"{service} is enabled but {serve_service.HEALTH_URL} did not answer; "
            f"repair: systemctl --user restart {service}; logs: journalctl --user -u {service}",
            restarted=restarted,
            **facts,
        )
    action = "restarted" if restarted else "enabled"
    return {
        "status": "applied",
        "note": f"{service} {action} and healthy; token source {result['token_source']}",
        "restarted": restarted,
        **facts,
    }


def _wait_for_serve(env: HostEnvironment) -> bool:
    for attempt in range(SERVE_HEALTH_ATTEMPTS):
        if _probe_serve(env)[0]:
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
    from verdict import (
        acceptance_service,
        docs_refresh_service,
        identify_service,
        pr_watcher_service,
        ref_docs_service,
    )

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
        if provider_id == REF_DOCS:
            steps = _install_timers(env, ref_docs_service.manage_service)
            return {"status": "applied", "note": f"{ref_docs_service.TIMER} enabled", **steps}
        if provider_id == DOCS_REFRESH:
            steps = _install_timers(env, docs_refresh_service.manage_service)
            note = f"{docs_refresh_service.TIMER} enabled"
            if not _docs_index_ready(env):
                # Start the refresh once, without waiting: it takes minutes.
                env.systemctl(
                    ["systemctl", "--user", "start", "--no-block", docs_refresh_service.SERVICE]
                )
                steps["refresh_started"] = docs_refresh_service.SERVICE
                note += f"; started {docs_refresh_service.SERVICE} (documentation index was stale)"
            return {"status": "applied", "note": note, **steps}
        if provider_id == PR_WATCHER:
            try:
                repos = _pr_watch_repos()
            except ValueError as exc:
                return {"status": "blocked", "note": str(exc)}
            if not repos:
                return {"status": "blocked", "note": _pr_watch_problem()}
            result = pr_watcher_service.manage_service(
                unit_dir=env.unit_dir, run=env.systemctl, repos=repos
            )
            return {
                "status": "applied",
                "note": f"{pr_watcher_service.TIMER} enabled",
                "plan": result.get("plan"),
                "changed": result.get("changed"),
            }
        if provider_id == CONTEXT_LIFT:
            from verdict import context_lift_service

            steps = _install_timers(env, context_lift_service.manage_service)
            return {"status": "applied", "note": f"{context_lift_service.TIMER} enabled", **steps}
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
    found = _inspect_ocr(env)
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
    healthy, why = _probe_serve(env)
    if healthy:
        health = _row("Verdict serve health", "ok", f"{HEALTH_URL} answers")
    else:
        health = _row(
            "Verdict serve health",
            "failed",
            f"{HEALTH_URL} did not answer{why}; repair: systemctl --user restart {SERVICE}; {logs}",
        )
    return [_row(label, "ok", f"{SERVICE} is active"), health]


def _prime_harness_row(env: HostEnvironment) -> dict[str, str]:
    """Prime is installed: is its Verdict harness enabled?

    Not enabled is a ``warning`` (shown with its repair, not an issue): live
    acceptance skips ``harness-prime`` then, so this row is where it shows.
    """
    from verdict.harness_prime import status

    label = "Prime harness"
    try:
        report = status(prime_home=env.prime_home)
    except (ValueError, OSError) as exc:  # JSONDecodeError is a ValueError
        # "unknown" is never ok and is a PROBLEM_STATE (shown with a repair), but
        # not blocking: the Prime registry row already fails for the same file.
        return _row(
            label,
            "unknown",
            f"harness state unknown: Prime config is unreadable ({type(exc).__name__}); "
            "fix the Prime registry row first, then "
            f"repair: {setup_repair(PRIME)}",
        )
    if report.enabled:
        return _row(
            label, "ok", f"{report.config_path}: Prime uses verdict serve {report.base_url}"
        )
    return _row(
        label,
        "warning",
        "Prime Agent installed but not enabled (optional harness); live acceptance "
        "skips harness-prime; repair: verdict harness prime enable",
    )


def _prime_rows(env: HostEnvironment) -> list[dict[str, str]]:
    from verdict.harness_prime import omniroute_registry_state

    label = "Prime registry"
    if not prime_installed(env):
        return [_row(label, NOT_INSTALLED, PRIME_OPTIONAL_DETAIL)]
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
    rows = [row, _prime_harness_row(env)]
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
