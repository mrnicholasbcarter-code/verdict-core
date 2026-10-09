"""Private local transaction fixtures; no home writes, probes or transport."""

from __future__ import annotations

import fcntl
import json
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from verdict import harness_prime_selection as selection
from verdict.harness_prime_compat import PrimeCompatibilityContext
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

NOW = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)
SECRET = "sk-transaction-private-do-not-render"


@pytest.fixture
def env(tmp_path: Path) -> dict[str, Any]:
    paths = SelectionPaths(tmp_path)
    old = (
        json.dumps(
            {
                "enabledModels": ["cx/old", "external", "historic/exact"],
                "defaultModel": "cx/old",
                "defaultProvider": "omniroute",
                "apiKey": SECRET,
                "nested": {"auth": SECRET, "keep": [1, 2]},
            }
        )
        + "  \n"
    ).encode()
    models = {
        "providers": {
            "omniroute": {
                "api": "openai-completions",
                "baseUrl": "http://127.0.0.1:20128/v1",
                "apiKey": SECRET,
                "models": [
                    {"id": "cx/gpt-6-sol", "name": "GPT", "thinkingLevelMap": {"high": "high"}},
                    {
                        "id": "cc/claude-opus-5-5",
                        "name": "Claude",
                        "api": "openai-responses",
                        "maxTokens": 8123,
                    },
                    {"id": "cx/old", "name": "Old", "api": "anthropic-messages"},
                ],
            },
            "openai": {"models": [{"id": "external", "name": "External"}], "apiKey": SECRET},
        }
    }
    registry_bytes = json.dumps(models).encode()
    paths.settings.write_bytes(old)
    paths.models.write_bytes(registry_bytes)
    os.chmod(paths.settings, 0o644)
    os.chmod(paths.models, 0o600)
    ctx = PrimeCompatibilityContext(
        tmp_path,
        True,
        "0.9.8",
        "fixture-binary-sha",
        "http://127.0.0.1:20128/v1",
        "fixture-source",
        True,
    )
    deps = PrimeDependencies(registry_bytes, ctx)
    rows = [
        {
            "route_id": rid,
            "provider": rid.split("/", 1)[0],
            "status": "VERIFIED",
            "checked_at": (NOW - timedelta(minutes=1)).isoformat(),
            "fresh_until": (NOW + timedelta(minutes=5)).isoformat(),
            "expires_at": (NOW + timedelta(minutes=10)).isoformat(),
            "evidence_source": "fixture-source",
            "identity": "verified",
            "coding_ok": False,
            "restrictions": [],
            "restriction": None,
            "cooldown_until": None,
        }
        for rid in ["cx/gpt-6-sol", "cc/claude-opus-5-5"]
    ]
    value = {"paths": paths, "old": old, "deps": deps, "rows": rows, "now": NOW, "models": models}
    value["plan"] = make_plan(value)
    return value


def make_plan(
    env: dict[str, Any], ids: tuple[str, ...] = ("cx/gpt-6-sol", "cc/claude-opus-5-5")
) -> selection.SelectionPreview:
    return preview_selection(
        load_settings(env["paths"].settings),
        env["rows"],
        selected_ids=ids,
        dependencies=env["deps"],
        now=env["now"],
    )


def apply(env: dict[str, Any], **kwargs: Any) -> selection.SelectionResult:
    arguments = {
        "expected_pre_digest": env["plan"].pre_digest,
        "confirmed": True,
        "paths": env["paths"],
        "reload_rows": lambda: env["rows"],
        "reload_dependencies": lambda: env["deps"],
        "clock": lambda: env["now"],
    }
    arguments.update(kwargs)
    return apply_selection(env["plan"], **arguments)


def recovery(env: dict[str, Any], transaction_id: str | None = None) -> selection.RestorePreview:
    _, receipt, backup = load_receipt(env["paths"], transaction_id=transaction_id)
    return preview_restore(load_settings(env["paths"].settings), receipt, backup_bytes=backup)


def restore(
    env: dict[str, Any], plan: selection.RestorePreview, **kwargs: Any
) -> selection.SelectionResult:
    arguments = {
        "expected_post_digest": plan.expected_post_digest,
        "confirmed": True,
        "paths": env["paths"],
        "clock": lambda: env["now"],
        "reload_dependencies": lambda: env["deps"],
    }
    arguments.update(kwargs)
    return restore_selection(plan, **arguments)


def owned(env: dict[str, Any], family: str = "select") -> list[Path]:
    return sorted(env["paths"].agent_dir.glob(f"settings.json.verdict-{family}-*"))


def test_preview_and_cancel_no_mutation(env: dict[str, Any]) -> None:
    before = {p.name: p.read_bytes() for p in env["paths"].agent_dir.iterdir()}
    assert not env["plan"].refusals
    for consent in [False, None, "yes", 1]:
        assert apply(env, confirmed=consent).status == "cancelled"
    assert before == {p.name: p.read_bytes() for p in env["paths"].agent_dir.iterdir()}
    assert not env["paths"].lock.exists()


def test_backup_multi_id_metadata_modes_receipts_restore(
    env: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    result = apply(env)
    assert result.status == "applied", result
    assert result.backup_path is not None and result.backup_path.read_bytes() == env["old"]
    assert result.receipt_path is not None
    receipt = json.loads(result.receipt_path.read_bytes())
    assert receipt["schema"] == selection.RECEIPT_SCHEMA
    assert receipt["status"] == "applied"
    assert receipt["pre_digest"] == byte_digest(env["old"])
    assert receipt["post_digest"] == byte_digest(env["paths"].settings.read_bytes())
    value = json.loads(env["paths"].settings.read_bytes())
    assert value["enabledModels"] == [
        "external",
        "historic/exact",
        "cx/gpt-6-sol",
        "cc/claude-opus-5-5",
    ]
    original = json.loads(env["old"])
    assert {k: v for k, v in value.items() if k != "enabledModels"} == {
        k: v for k, v in original.items() if k != "enabledModels"
    }
    assert json.loads(env["paths"].models.read_bytes()) == env["models"]
    assert env["paths"].models.read_bytes() == env["deps"].registry_bytes
    assert SECRET not in result.receipt_path.read_text() + repr(result) + json.dumps(
        result.to_dict()
    )
    restore_plan = recovery(env)
    assert not restore_plan.refusals
    assert SECRET not in repr(restore_plan) + json.dumps(restore_plan.to_dict())
    post = env["paths"].settings.read_bytes()
    undo = restore(env, restore_plan)
    assert undo.status == "restored", undo
    assert env["paths"].settings.read_bytes() == env["old"]
    assert undo.backup_path is not None and undo.backup_path.read_bytes() == post
    assert result.backup_path.exists() and result.receipt_path.exists()
    for path in [env["paths"].settings, env["paths"].lock, *owned(env), *owned(env, "restore")]:
        assert path.stat().st_mode & 0o777 == 0o600
    assert capsys.readouterr().out + capsys.readouterr().err == ""


@pytest.mark.parametrize("change", [b"\n", b" ", b"\n\t"])
def test_concurrent_byte_edit_refused_before_backup(env: dict[str, Any], change: bytes) -> None:
    later = env["old"] + change
    env["paths"].settings.write_bytes(later)
    result = apply(env)
    assert result.status == "refused" and result.reasons == ("config_changed",)
    assert env["paths"].settings.read_bytes() == later
    assert not owned(env)


def test_same_bytes_replaced_inode_refused(env: dict[str, Any]) -> None:
    temp = env["paths"].settings.with_name("user-temp")
    temp.write_bytes(env["old"])
    temp.chmod(0o600)
    temp.replace(env["paths"].settings)
    assert apply(env).reasons == ("config_changed",)
    assert not owned(env)


@pytest.mark.parametrize("dependency", ["registry", "project", "credentials", "binary"])
def test_dependency_change_refused(env: dict[str, Any], dependency: str) -> None:
    if dependency == "registry":
        env["paths"].models.write_bytes(env["deps"].registry_bytes + b" ")
    elif dependency == "project":
        env["deps"] = replace(env["deps"], project_digest="changed")
    elif dependency == "credentials":
        env["deps"] = replace(
            env["deps"], discovery=replace(env["deps"].discovery, credentials_present=False)
        )
    else:
        env["deps"] = replace(
            env["deps"], discovery=replace(env["deps"].discovery, binary_digest="changed")
        )
    assert apply(env).status == "refused"
    assert not owned(env)
    assert env["paths"].settings.read_bytes() == env["old"]


def test_project_file_reread_and_override_warning(env: dict[str, Any]) -> None:
    project = env["paths"].agent_dir / "project" / "settings.json"
    project.parent.mkdir(mode=0o700)
    project.write_bytes(b'{"enabledModels":["external"]}')
    project.chmod(0o600)
    env["paths"] = replace(env["paths"], project_settings=project)
    env["deps"] = replace(
        env["deps"],
        project_digest=byte_digest(project.read_bytes()),
        discovery=replace(env["deps"].discovery, project_scope_override=True),
    )
    env["plan"] = make_plan(env)
    assert "current_project_enabledModels_overrides_global_scope" in env["plan"].warnings
    project.write_bytes(b'{"enabledModels": ["external"]}')
    assert apply(env).reasons == ("dependencies_changed",)
    assert not owned(env)


def test_changed_proof_refused_and_old_deadline_cannot_extend(env: dict[str, Any]) -> None:
    env["rows"][0]["status"] = "STALE"
    assert apply(env).reasons == ("proof_or_compatibility_changed",)
    env["rows"][0]["status"] = "VERIFIED"
    env["now"] = NOW + timedelta(minutes=5)
    for row in env["rows"]:
        row["checked_at"] = env["now"].isoformat()
        row["fresh_until"] = (env["now"] + timedelta(minutes=5)).isoformat()
        row["expires_at"] = (env["now"] + timedelta(minutes=10)).isoformat()
    assert apply(env).reasons == ("proof_deadline_elapsed_new_preview_required",)
    assert not owned(env)


def test_digest_confirmation_guard(env: dict[str, Any]) -> None:
    assert apply(env, expected_pre_digest="wrong").reasons == ("pre_digest_mismatch",)
    assert not env["paths"].lock.exists()
    assert not owned(env)


def test_busy_nonblocking(env: dict[str, Any]) -> None:
    fd = os.open(env["paths"].lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert apply(env).status == "busy"
        assert env["paths"].settings.read_bytes() == env["old"]
        assert not owned(env)
    finally:
        os.close(fd)


@pytest.mark.parametrize("target", ["settings", "models", "lock"])
def test_symlink_refusal(env: dict[str, Any], target: str) -> None:
    path = getattr(env["paths"], target)
    other = path.with_name("untouched-" + target)
    if path.exists():
        path.rename(other)
    else:
        other.write_bytes(b"")
    path.symlink_to(other)
    result = apply(env)
    assert result.status == "refused"
    assert not owned(env)
    if target == "settings":
        with pytest.raises(PrimeSelectionError):
            load_settings(path)


def test_private_lock_mode_refusal(env: dict[str, Any]) -> None:
    env["paths"].lock.write_bytes(b"")
    env["paths"].lock.chmod(0o644)
    assert apply(env).reasons == ("unsafe_lock",)


def test_idempotence_still_validates_proof_no_backup(env: dict[str, Any]) -> None:
    assert apply(env).status == "applied"
    # The OLD preview is not reusable; a new identical plan is the no-op path.
    assert apply(env).reasons == ("config_changed",)
    env["plan"] = make_plan(env)
    before = {p.name: (p.read_bytes(), p.stat().st_ino) for p in env["paths"].agent_dir.iterdir()}
    assert apply(env).status == "unchanged"
    assert before == {
        p.name: (p.read_bytes(), p.stat().st_ino) for p in env["paths"].agent_dir.iterdir()
    }
    env["rows"][0]["status"] = "UNVERIFIED"
    assert apply(env).status == "refused"


def test_restore_cancel_and_later_user_edit_refused(env: dict[str, Any]) -> None:
    assert apply(env).status == "applied"
    undo_plan = recovery(env)
    assert restore(env, undo_plan, confirmed=False).status == "cancelled"
    assert not owned(env, "restore")
    later = env["paths"].settings.read_bytes() + b"\n"
    env["paths"].settings.write_bytes(later)
    assert restore(env, undo_plan).reasons == ("config_changed",)
    assert recovery(env).refusals == ("config_changed",)
    assert env["paths"].settings.read_bytes() == later
    assert not owned(env, "restore")


def test_restore_idempotence_and_post_digest_guard(env: dict[str, Any]) -> None:
    assert apply(env).status == "applied"
    undo_plan = recovery(env)
    assert restore(env, undo_plan, expected_post_digest="wrong").reasons == (
        "post_digest_mismatch",
    )
    assert restore(env, undo_plan).status == "restored"
    prior_files = {p.name: p.read_bytes() for p in env["paths"].agent_dir.iterdir()}
    assert restore(env, undo_plan).status == "unchanged"
    assert restore(env, recovery(env)).status == "unchanged"
    assert prior_files == {p.name: p.read_bytes() for p in env["paths"].agent_dir.iterdir()}
    env["paths"].settings.write_bytes(env["old"] + b" ")
    assert restore(env, undo_plan).status == "refused"


def test_restore_does_not_require_historical_health(env: dict[str, Any]) -> None:
    assert apply(env).status == "applied"
    undo_plan = recovery(env)
    env["now"] = NOW + timedelta(days=1)
    env["rows"] = []
    assert restore(env, undo_plan).status == "restored"


def test_backup_tamper_and_receipt_traversal_refused(env: dict[str, Any]) -> None:
    result = apply(env)
    assert result.backup_path is not None and result.receipt_path is not None
    undo_plan = recovery(env)
    result.backup_path.write_bytes(env["old"] + b" ")
    assert restore(env, undo_plan).reasons == ("backup_digest_mismatch",)
    with pytest.raises(PrimeSelectionError, match="transaction_id_invalid"):
        load_receipt(env["paths"], transaction_id="../../settings")
    result.backup_path.write_bytes(env["old"])
    receipt = json.loads(result.receipt_path.read_bytes())
    receipt["backup_name"] = "../../settings.json"
    result.receipt_path.write_bytes(json.dumps(receipt).encode())
    assert restore(env, undo_plan).reasons == ("receipt_invalid",)


def test_mutation_during_preparation_retains_recovery_and_cleans_temp(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    original = selection._private_write
    later = env["old"] + b"\n"

    def mutate(path: Path, raw: bytes) -> None:
        original(path, raw)
        if path.suffix == ".tmp":
            env["paths"].settings.write_bytes(later)

    monkeypatch.setattr(selection, "_private_write", mutate)
    result = apply(env)
    assert result.status == "refused" and result.reasons == ("config_changed",)
    assert env["paths"].settings.read_bytes() == later
    assert result.backup_path is not None and result.backup_path.read_bytes() == env["old"]
    assert not list(env["paths"].agent_dir.glob(".*.tmp"))
    assert result.receipt_path is not None
    assert json.loads(result.receipt_path.read_bytes())["status"] == "prepared"


def test_proof_expiry_immediately_before_replace(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    original = selection._private_write

    def expire(path: Path, raw: bytes) -> None:
        original(path, raw)
        if path.suffix == ".tmp":
            env["now"] = NOW + timedelta(minutes=5)

    monkeypatch.setattr(selection, "_private_write", expire)
    result = apply(env)
    assert result.reasons == ("proof_deadline_elapsed_new_preview_required",)
    assert env["paths"].settings.read_bytes() == env["old"]
    assert not list(env["paths"].agent_dir.glob(".*.tmp"))


def test_replace_failure_never_exposes_partial_settings(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(src: Path, dst: Path) -> None:
        assert src != env["paths"].settings
        assert src.stat().st_mode & 0o777 == 0o600
        assert json.loads(src.read_bytes())["enabledModels"] == list(env["plan"].after)
        raise OSError("secret " + SECRET)

    monkeypatch.setattr(selection.os, "replace", fail)
    result = apply(env)
    assert result.status == "refused"
    assert result.reasons == ("private_transaction_io_failed",)
    assert SECRET not in repr(result) + json.dumps(result.to_dict())
    assert env["paths"].settings.read_bytes() == env["old"]
    assert not list(env["paths"].agent_dir.glob(".*.tmp"))


def test_receipt_finalization_failure_is_recoverable_applied(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    original = selection.os.replace

    def fail_receipt(src: Path, dst: Path) -> None:
        if dst.name.endswith(".receipt.json"):
            raise OSError(SECRET)
        original(src, dst)

    monkeypatch.setattr(selection.os, "replace", fail_receipt)
    result = apply(env)
    assert result.status == "applied_receipt_incomplete"
    assert env["paths"].settings.read_bytes() != env["old"]
    assert result.receipt_path is not None
    assert json.loads(result.receipt_path.read_bytes())["status"] == "prepared"
    assert not list(env["paths"].agent_dir.glob(".*.tmp"))
    monkeypatch.setattr(selection.os, "replace", original)
    undo_plan = recovery(env)
    assert restore(env, undo_plan).status == "restored"


def test_retention_only_completed_owned_pairs(env: dict[str, Any]) -> None:
    other = env["paths"].agent_dir / "models.json.verdict-sync-user.bak"
    other.write_bytes(b"owner-file")
    unknown = env["paths"].agent_dir / "settings.json.verdict-select-owner.bak"
    unknown.write_bytes(b"owner-file")
    orphan = env["paths"].agent_dir / (
        "settings.json.verdict-select-20261008T120000000000Z-" + "f" * 32 + ".bak"
    )
    orphan.write_bytes(env["old"])
    orphan.chmod(0o600)
    for index in range(7):
        env["now"] = NOW + timedelta(seconds=index)
        env["plan"] = make_plan(env, ("cx/gpt-6-sol",) if index % 2 else ("cc/claude-opus-5-5",))
        assert apply(env).status == "applied"
    assert (
        len(list(env["paths"].agent_dir.glob("settings.json.verdict-select-*.receipt.json"))) == 5
    )
    assert (
        len(list(env["paths"].agent_dir.glob("settings.json.verdict-select-*.bak"))) == 7
    )  # 5 pairs + orphan + owner
    assert other.read_bytes() == b"owner-file" and unknown.read_bytes() == b"owner-file"
    assert orphan.exists()


def test_restore_retention_preserves_selection_pairs(env: dict[str, Any]) -> None:
    for index in range(6):
        env["now"] = NOW + timedelta(seconds=index)
        env["plan"] = make_plan(env)
        assert apply(env).status == "applied"
        assert restore(env, recovery(env)).status == "restored"
    assert (
        len(list(env["paths"].agent_dir.glob("settings.json.verdict-restore-*.receipt.json"))) == 5
    )
    assert len(list(env["paths"].agent_dir.glob("settings.json.verdict-restore-*.bak"))) == 5
    assert (
        len(list(env["paths"].agent_dir.glob("settings.json.verdict-select-*.receipt.json"))) == 5
    )


def test_prepared_receipt_pre_digest_means_not_applied(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(src: Path, dst: Path) -> None:
        raise OSError("crash before rename")

    monkeypatch.setattr(selection.os, "replace", fail)
    assert apply(env).status == "refused"
    with pytest.raises(PrimeSelectionError, match="selection_receipt_missing"):
        load_receipt(env["paths"])


def test_plan_dataclass_tampering_is_refused(env: dict[str, Any]) -> None:
    env["plan"] = replace(env["plan"], after=("cx/not-approved",))
    assert apply(env).reasons == ("plan_changed",)
    assert not env["paths"].lock.exists()


def test_callback_mutation_caught_before_noop_or_write(env: dict[str, Any]) -> None:
    later = env["old"] + b" "

    def mutate_rows() -> list[dict[str, Any]]:
        env["paths"].settings.write_bytes(later)
        return env["rows"]

    assert apply(env, reload_rows=mutate_rows).reasons == ("config_changed",)
    assert env["paths"].settings.read_bytes() == later
    assert not owned(env)


def test_restore_final_check_preserves_user_edit(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert apply(env).status == "applied"
    undo_plan = recovery(env)
    post = env["paths"].settings.read_bytes()
    later = post + b" "
    original = selection._private_write

    def mutate(path: Path, raw: bytes) -> None:
        original(path, raw)
        if path.suffix == ".tmp":
            env["paths"].settings.write_bytes(later)

    monkeypatch.setattr(selection, "_private_write", mutate)
    result = restore(env, undo_plan)
    assert result.status == "refused" and result.reasons == ("config_changed",)
    assert env["paths"].settings.read_bytes() == later
    assert result.backup_path is not None and result.backup_path.read_bytes() == post
    assert not list(env["paths"].agent_dir.glob(".*.tmp"))


def test_hard_crash_during_temp_preparation_keeps_settings_private(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    original = selection._private_write

    def crash(path: Path, raw: bytes) -> None:
        original(path, raw)
        if path.suffix == ".tmp":
            raise SystemExit("simulated crash")

    monkeypatch.setattr(selection, "_private_write", crash)
    with pytest.raises(SystemExit):
        apply(env)
    assert env["paths"].settings.read_bytes() == env["old"]
    assert not list(env["paths"].agent_dir.glob(".*.tmp"))
    assert len(owned(env)) == 2


def test_prepared_recovery_other_digest_refuses(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    original = selection.os.replace

    def fail(src: Path, dst: Path) -> None:
        if dst.name.endswith(".receipt.json"):
            raise OSError("crash")
        original(src, dst)

    monkeypatch.setattr(selection.os, "replace", fail)
    assert apply(env).status == "applied_receipt_incomplete"
    monkeypatch.setattr(selection.os, "replace", original)
    env["paths"].settings.write_bytes(env["paths"].settings.read_bytes() + b" ")
    undo_plan = recovery(env)
    assert "config_changed" in undo_plan.refusals
    assert restore(env, undo_plan).status == "refused"


def test_incomplete_and_unmatched_pairs_not_pruned(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    original = selection.os.replace

    def fail(src: Path, dst: Path) -> None:
        raise OSError("crash before rename")

    monkeypatch.setattr(selection.os, "replace", fail)
    failed = apply(env)
    assert failed.status == "refused"
    assert failed.backup_path is not None and failed.receipt_path is not None
    monkeypatch.setattr(selection.os, "replace", original)
    for index in range(7):
        env["now"] = NOW + timedelta(seconds=index)
        env["plan"] = make_plan(env, ("cx/gpt-6-sol",) if index % 2 else ("cc/claude-opus-5-5",))
        assert apply(env).status == "applied"
    assert failed.backup_path.exists() and failed.receipt_path.exists()
    assert json.loads(failed.receipt_path.read_bytes())["status"] == "prepared"
    assert (
        len(list(env["paths"].agent_dir.glob("settings.json.verdict-select-*.receipt.json"))) == 6
    )


def test_restore_plan_tampering_refused(env: dict[str, Any]) -> None:
    assert apply(env).status == "applied"
    plan = recovery(env)
    altered = replace(plan, expected_post_digest=byte_digest(env["old"]))
    assert restore(env, altered).reasons == ("plan_changed",)
    assert not owned(env, "restore")


def test_directory_fsync_failure_reports_applied_incomplete(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    original = selection._sync_dir
    calls = 0

    def fail_after_replace(path: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated durability failure")
        original(path)

    monkeypatch.setattr(selection, "_sync_dir", fail_after_replace)
    result = apply(env)
    assert result.status == "applied_receipt_incomplete"
    assert result.post_digest == byte_digest(env["paths"].settings.read_bytes())
    monkeypatch.setattr(selection, "_sync_dir", original)
    assert restore(env, recovery(env)).status == "restored"


def test_absent_project_settings_under_group_writable_parent_applies(env: dict[str, Any]) -> None:
    """A realistic 0775-umask ``.prime`` dir with no project settings must not refuse.

    Before the fix, ``_safe_parent`` checked the parent directory's mode before
    the open ever ran, so a merely-absent file under a group/other-writable
    parent raised ``unsafe_directory`` instead of being treated as "no project
    override". The optional project settings path must tolerate this.
    """
    project_dir = env["paths"].agent_dir / "cwd-prime"
    project_dir.mkdir()
    project_dir.chmod(0o775)  # explicit: mkdir(mode=) is masked by the runner umask (0022 on CI)
    project = project_dir / "settings.json"
    assert not project.exists()
    env["paths"] = replace(env["paths"], project_settings=project)
    env["plan"] = make_plan(env)
    assert env["plan"].refusals == ()
    assert apply(env).status == "applied"


def test_present_project_settings_under_group_writable_parent_still_refused(
    env: dict[str, Any],
) -> None:
    """A *present* project file keeps full safety checks regardless of absence."""
    project_dir = env["paths"].agent_dir / "cwd-prime2"
    project_dir.mkdir()
    project_dir.chmod(0o775)  # explicit: mkdir(mode=) is masked by the runner umask (0022 on CI)
    project = project_dir / "settings.json"
    project.write_bytes(b'{"enabledModels": []}')
    project.chmod(0o644)
    env["paths"] = replace(env["paths"], project_settings=project)
    env["deps"] = replace(env["deps"], project_digest=byte_digest(project.read_bytes()))
    env["plan"] = make_plan(env)
    assert apply(env).reasons == ("unsafe_directory",)
    assert not owned(env)


def test_project_settings_appearing_after_preview_refuses_dependencies_changed(
    env: dict[str, Any],
) -> None:
    """A project file created between preview and apply must change the digest."""
    project_dir = env["paths"].agent_dir / "cwd-prime3"
    project_dir.mkdir(mode=0o700)
    project = project_dir / "settings.json"
    assert not project.exists()
    env["paths"] = replace(env["paths"], project_settings=project)
    env["plan"] = make_plan(env)
    assert env["plan"].refusals == ()
    project.write_bytes(b'{"enabledModels": ["external"]}')
    project.chmod(0o600)
    assert apply(env).reasons == ("dependencies_changed",)
    assert not owned(env)


def test_project_parent_symlink_with_absent_target_refused(env: dict[str, Any]) -> None:
    """A parent symlink must not let an absent leaf look like "no project override".

    Before this fix, ``_project_digest`` lstat'd only the exact leaf path, so a
    project dir that is itself a symlink (even to a real, existing directory)
    with no ``settings.json`` inside it returned ``None`` (tolerated) instead
    of going through the same ancestor-symlink refusal ``_safe_parent`` uses.
    """
    real_dir = env["paths"].agent_dir / "real-cwd-prime"
    real_dir.mkdir(mode=0o700)
    link_dir = env["paths"].agent_dir / "linked-cwd-prime"
    link_dir.symlink_to(real_dir, target_is_directory=True)
    project = link_dir / "settings.json"
    assert not project.exists()
    env["paths"] = replace(env["paths"], project_settings=project)
    env["plan"] = make_plan(env)
    assert apply(env).reasons == ("unsafe_path",)
    assert not owned(env)


def test_project_parent_symlink_dangling_refused(env: dict[str, Any]) -> None:
    """A dangling parent symlink must also be refused, not treated as absent."""
    missing_target = env["paths"].agent_dir / "does-not-exist"
    link_dir = env["paths"].agent_dir / "dangling-cwd-prime"
    link_dir.symlink_to(missing_target, target_is_directory=True)
    project = link_dir / "settings.json"
    assert not project.exists()
    env["paths"] = replace(env["paths"], project_settings=project)
    env["plan"] = make_plan(env)
    assert apply(env).reasons == ("unsafe_path",)
    assert not owned(env)


def test_project_ancestor_symlink_two_levels_up_refused(env: dict[str, Any]) -> None:
    """A symlink higher in the chain (not the direct parent) must also refuse."""
    real_root = env["paths"].agent_dir / "real-root"
    real_root.mkdir(mode=0o700)
    link_root = env["paths"].agent_dir / "linked-root"
    link_root.symlink_to(real_root, target_is_directory=True)
    nested = link_root / "nested"
    nested.mkdir(mode=0o700)
    project = nested / "settings.json"
    assert not project.exists()
    env["paths"] = replace(env["paths"], project_settings=project)
    env["plan"] = make_plan(env)
    assert apply(env).reasons == ("unsafe_path",)
    assert not owned(env)


def test_absent_leaf_under_real_nested_group_writable_dir_applies(env: dict[str, Any]) -> None:
    """A real (non-symlinked) ancestor chain with an absent leaf still applies."""
    nested = env["paths"].agent_dir / "real-nested" / "cwd-prime"
    nested.mkdir(mode=0o700, parents=True)
    nested.chmod(0o775)
    project = nested / "settings.json"
    assert not project.exists()
    env["paths"] = replace(env["paths"], project_settings=project)
    env["plan"] = make_plan(env)
    assert env["plan"].refusals == ()
    assert apply(env).status == "applied"
