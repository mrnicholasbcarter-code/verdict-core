"""Guarded Prime interactive ``/enabledModels`` selection and byte restore.

Pure plans display only scoped ids, stable reasons and digests. Private documents
and backups can contain credentials; their repr and public ``to_dict`` never do.
Only literal confirmation commits. All paths, local evidence reloads and clocks
are supplied by the caller; refresh/network/credential helpers MUST finish before
this API. Neither selection nor recovery grants runtime launch admission.

Writers cooperate on a nonblocking private advisory flock. Digest/inode rechecks
catch observed external edits, but cannot prevent the tiny check-to-rename race
with a non-cooperating editor. Do not edit settings concurrently. Unknown locking
platforms fail closed. JSON whitespace normalizes on selection; restore is exact.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from verdict.admission import canonical_route_id
from verdict.harness_prime_compat import (
    PrimeCompatibilityContext,
    PrimeSelectionRow,
    aware_time,
    bind_prime_token,
    exact_token,
    prime_selection_rows,
    timestamp,
)

SCHEMA = "verdict.prime-selection/v1"
RECEIPT_SCHEMA = "verdict.prime-selection-receipt/v1"
RESTORE_RECEIPT_SCHEMA = "verdict.prime-restore-receipt/v1"
POLICY = "replace_unique_omniroute_interactive_scope"
_OWNED = re.compile(
    r"settings\.json\.verdict-(select|restore)-(\d{8}T\d{12}Z)-([a-f0-9]{32})\.receipt\.json\Z"
)
FileIdentity = tuple[int, int, int, int, int, int]


class PrimeSelectionError(ValueError):
    """A stable, secret-free refusal code, not an underlying OS/JSON error.

    ``path`` (when set) is the specific unsafe filesystem path a caller may
    want to render as an actionable hint. ``str(exc)`` stays exactly the code;
    nothing here changes what callers already match on.
    """

    def __init__(self, code: str, *, path: Path | None = None) -> None:
        super().__init__(code)
        self.path = path


def byte_digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PrimeSelectionError("duplicate_json_key")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise PrimeSelectionError("invalid_json")


def _json(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_invalid_constant)
    except (ValueError, UnicodeError) as exc:
        if isinstance(exc, PrimeSelectionError):
            raise
        raise PrimeSelectionError("invalid_json") from None
    if not isinstance(value, dict):
        raise PrimeSelectionError("json_object_required")
    return value


def _encode(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(dict(value), indent=2, ensure_ascii=True, allow_nan=False) + "\n").encode()


def _digest_value(value: Mapping[str, Any]) -> str:
    return byte_digest(json.dumps(dict(value), sort_keys=True, separators=(",", ":")).encode())


@dataclass(frozen=True)
class SettingsDocument:
    """Exact settings preimage. ``from_bytes`` is pure; ``load_settings`` does I/O."""

    path: Path
    raw_bytes: bytes = field(repr=False)
    file_identity: FileIdentity | None = None

    @classmethod
    def from_bytes(
        cls, path: Path, raw_bytes: bytes, *, file_identity: FileIdentity | None = None
    ) -> SettingsDocument:
        _scope(_json(raw_bytes))
        return cls(Path(path), raw_bytes, file_identity)

    @property
    def pre_digest(self) -> str:
        return byte_digest(self.raw_bytes)

    @property
    def value(self) -> dict[str, Any]:
        return _json(self.raw_bytes)


@dataclass(frozen=True)
class PrimeDependencies:
    """Raw registry and project digests plus supplied sanitized discovery.

    Use actual registry bytes (not reserialized JSON) and reload the same sources
    under the lock. No auth document is needed. The context's endpoint/source and
    credential booleans are hashed, never exposed in preview output.
    """

    registry_bytes: bytes = field(repr=False)
    discovery: PrimeCompatibilityContext = field(repr=False)
    project_digest: str | None = None

    @property
    def models_doc(self) -> dict[str, Any]:
        return _json(self.registry_bytes)

    @property
    def registry_digest(self) -> str:
        return byte_digest(self.registry_bytes)

    def digests(self) -> dict[str, str | None]:
        ctx = self.discovery
        return {
            "registry": self.registry_digest,
            "project": self.project_digest,
            "discovery": _digest_value(
                {
                    "agent_dir": str(ctx.agent_dir),
                    "installed": ctx.installed,
                    "release": ctx.release,
                    "binary_digest": ctx.binary_digest,
                    "gateway_endpoint": ctx.gateway_endpoint,
                    "evidence_source": ctx.evidence_source,
                    "credentials_present": ctx.credentials_present,
                    "credential_overrides": ctx.credential_overrides,
                    "project_scope_override": ctx.project_scope_override,
                    "path_recognized": ctx.path_recognized,
                }
            ),
        }


@dataclass(frozen=True)
class SelectionPaths:
    """Resolved operator global dir and optional effective project settings path."""

    agent_dir: Path
    project_settings: Path | None = None

    @property
    def settings(self) -> Path:
        return self.agent_dir / "settings.json"

    @property
    def models(self) -> Path:
        return self.agent_dir / "models.json"

    @property
    def lock(self) -> Path:
        return self.agent_dir / ".verdict-prime-selection.lock"


@dataclass(frozen=True)
class SelectionPreview:
    target: Path
    selected_ids: tuple[str, ...]
    before: tuple[str, ...]
    after: tuple[str, ...]
    added: tuple[str, ...]
    removed: tuple[str, ...]
    retained: tuple[str, ...]
    warnings: tuple[str, ...]
    refusals: tuple[str, ...]
    pre_digest: str
    dependency_digests: tuple[tuple[str, str | None], ...]
    proof_deadline: datetime | None
    rows: tuple[PrimeSelectionRow, ...]
    _settings: SettingsDocument = field(repr=False)
    _dependencies: PrimeDependencies = field(repr=False)
    _projection_bytes: bytes = field(repr=False)
    _generated_at: datetime = field(repr=False)

    @property
    def plan_digest(self) -> str:
        return _digest_value(
            {**self.to_dict(include_plan=False), "file_identity": self._settings.file_identity}
        )

    def to_dict(self, *, include_plan: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema": SCHEMA,
            "target": str(self.target),
            "key": "/enabledModels",
            "policy": POLICY,
            "mode": "interactive",
            "selected_ids": list(self.selected_ids),
            "before": list(self.before),
            "after": list(self.after),
            "added": list(self.added),
            "removed": list(self.removed),
            "retained": list(self.retained),
            "warnings": list(self.warnings),
            "refusals": list(self.refusals),
            "pre_digest": self.pre_digest,
            "dependency_digests": dict(self.dependency_digests),
            "proof_deadline": self.proof_deadline.isoformat() if self.proof_deadline else None,
            "rows": [r.to_dict() for r in self.rows],
        }
        if include_plan:
            result["plan_digest"] = self.plan_digest
        return result


@dataclass(frozen=True)
class SelectionResult:
    status: str
    reasons: tuple[str, ...] = ()
    transaction_id: str | None = None
    backup_path: Path | None = None
    receipt_path: Path | None = None
    pre_digest: str | None = None
    post_digest: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reasons": list(self.reasons),
            "transaction_id": self.transaction_id,
            "backup_path": str(self.backup_path) if self.backup_path else None,
            "receipt_path": str(self.receipt_path) if self.receipt_path else None,
            "pre_digest": self.pre_digest,
            "post_digest": self.post_digest,
        }


def _scope(value: Mapping[str, Any]) -> tuple[str, ...]:
    scope = value.get("enabledModels", [])
    if not isinstance(scope, list) or any(not isinstance(t, str) for t in scope):
        raise PrimeSelectionError("enabled_models_type_invalid")
    return tuple(scope)


def preview_selection(
    settings: SettingsDocument,
    projection_rows: Sequence[Mapping[str, Any]],
    *,
    selected_ids: Sequence[str],
    dependencies: PrimeDependencies,
    now: datetime,
) -> SelectionPreview:
    """Pure exact scoped diff; no refresh, file/clock reads, locks or backups.

    Retained unknowns are not verified. Missing visibility offers separate
    ``verdict harness prime sync-models --dry-run``; this API never invokes it.
    Raw projection error/restriction text is untrusted and never rendered.
    """
    aware_time(now)
    before_raw = _scope(settings.value)
    models = dependencies.models_doc
    refusals: list[str] = []
    warnings = [
        "interactive_Alt+M_only_not_launch_permission",
        "project_settings_and_explicit_models_can_override",
        "existing_sessions_may_need_scoped_models_or_restart",
        "selection_normalizes_JSON_whitespace_only",
    ]
    ctx = dependencies.discovery
    if settings.path != ctx.agent_dir / "settings.json" or not ctx.path_recognized:
        refusals.append("effective_path_unrecognized")
    if not ctx.installed:
        refusals.append("binary_missing")
    if ctx.project_scope_override:
        warnings.append("current_project_enabledModels_overrides_global_scope")
    selected: list[str] = []
    for token in selected_ids:
        if not exact_token(token):
            refusals.append("selected_identity_unsupported")
            continue
        rid = canonical_route_id(token)
        if not exact_token(rid):
            refusals.append("selected_identity_unsupported")
        elif rid not in selected:
            selected.append(rid)
    if not selected:
        refusals.append("nonempty_selection_required")
    retained: list[str] = []
    before: list[str] = []
    for token in before_raw:
        binding = bind_prime_token(token, models)
        before.append(binding.token)
        if binding.disposition == "unsafe":
            refusals.extend(binding.reasons)
            warnings.append("resolve_unsafe_scope_through_Prime_/scoped-models")
        elif binding.registry_provider != "omniroute":
            retained.append(token)
            if binding.disposition == "retained_unknown":
                warnings.append("retained_unknown_not_verified_not_a_full_verified_allowlist")
            else:
                warnings.append(
                    "retained_other_provider_not_verified_not_a_full_verified_allowlist"
                )
    needed_rows = [
        r
        for r in projection_rows
        if exact_token(r.get("route_id")) and canonical_route_id(str(r["route_id"])) in selected
    ]
    overlay = prime_selection_rows(models, needed_rows, context=ctx, now=now)
    chosen: list[PrimeSelectionRow] = []
    deadlines: list[datetime] = []
    for rid in selected:
        matches = [r for r in overlay if r.route_id == rid]
        if len(matches) != 1:
            refusals.append(f"{rid}:projection_missing_or_ambiguous")
            continue
        row = matches[0]
        chosen.append(row)
        if not row.selectable:
            refusals.extend(f"{rid}:{reason}" for reason in row.reasons)
        fresh = timestamp(row.fresh_until)
        if fresh is not None:
            deadlines.append(fresh)
    after = tuple([*retained, *selected])
    default = settings.value.get("defaultModel")
    if isinstance(default, str) and default not in after:
        warnings.append("defaultModel_outside_scope_unchanged")
    return SelectionPreview(
        target=settings.path,
        selected_ids=tuple(selected),
        before=tuple(before),
        after=after,
        added=tuple(t for t in after if t not in before_raw),
        removed=tuple(t for t in before if t not in after),
        retained=tuple(retained),
        warnings=tuple(dict.fromkeys(warnings)),
        refusals=tuple(dict.fromkeys(refusals)),
        pre_digest=settings.pre_digest,
        dependency_digests=tuple(dependencies.digests().items()),
        proof_deadline=min(deadlines) if len(deadlines) == len(selected) and deadlines else None,
        rows=tuple(chosen),
        _settings=settings,
        _dependencies=dependencies,
        _projection_bytes=_encode({"rows": list(projection_rows)}),
        _generated_at=now,
    )


def _identity(s: os.stat_result) -> FileIdentity:
    return (s.st_dev, s.st_ino, s.st_uid, s.st_mode, s.st_size, s.st_mtime_ns)


def _safe_parent(path: Path) -> None:
    if not path.is_absolute() or any(p.is_symlink() for p in (path.parent, *path.parents)):
        raise PrimeSelectionError("unsafe_path", path=path)
    info = path.parent.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o022 or not stat.S_ISDIR(info.st_mode):
        raise PrimeSelectionError("unsafe_directory", path=path.parent)


def _read(path: Path, *, private: bool = False) -> tuple[bytes, FileIdentity]:
    _safe_parent(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o022
            or info.st_nlink != 1
            or (private and stat.S_IMODE(info.st_mode) != 0o600)
        ):
            raise PrimeSelectionError("unsafe_file", path=path)
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read()
        if _identity(os.fstat(fd)) != _identity(info) or _identity(path.lstat()) != _identity(info):
            raise PrimeSelectionError("config_changed")
        return raw, _identity(info)
    finally:
        os.close(fd)


def load_settings(path: Path) -> SettingsDocument:
    """Read a regular owner-controlled settings document without following links."""
    try:
        raw, identity = _read(path)
        return SettingsDocument.from_bytes(path, raw, file_identity=identity)
    except OSError:
        raise PrimeSelectionError("settings_missing_or_unsafe") from None


@contextmanager
def _lock(paths: SelectionPaths) -> Iterator[None]:
    try:
        import fcntl
    except ImportError:
        raise PrimeSelectionError("locking_unsupported") from None
    _safe_parent(paths.lock)
    fd = os.open(paths.lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise PrimeSelectionError("unsafe_lock", path=paths.lock)
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise PrimeSelectionError("unsafe_lock", path=paths.lock)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PrimeSelectionError("busy") from None
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _project_digest(path: Path) -> str | None:
    """Digest an optional project settings file; absence needs no safe parent.

    ``lstat`` on the exact path runs before any parent-directory mode check, so
    a missing file under a group/other-writable project directory (a realistic
    0775-umask checkout) is tolerated, not refused. A present file still goes
    through ``_read``'s full ``_safe_parent``/ownership/mode checks unchanged.
    """
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    project, _ = _read(path)
    _json(project)
    return byte_digest(project)


def _verify_dependencies(dependencies: PrimeDependencies, paths: SelectionPaths) -> None:
    if dependencies.discovery.agent_dir != paths.agent_dir or not dependencies.discovery.installed:
        raise PrimeSelectionError("discovery_changed")
    if not dependencies.discovery.path_recognized:
        raise PrimeSelectionError("effective_path_unrecognized")
    registry, _ = _read(paths.models)
    _json(registry)
    if byte_digest(registry) != dependencies.registry_digest:
        raise PrimeSelectionError("dependencies_changed")
    if paths.project_settings is None:
        if dependencies.project_digest is not None:
            raise PrimeSelectionError("project_path_required")
    else:
        if _project_digest(paths.project_settings) != dependencies.project_digest:
            raise PrimeSelectionError("dependencies_changed")


def _check_current(current: SettingsDocument, approved: SettingsDocument) -> None:
    if current.pre_digest != approved.pre_digest or (
        approved.file_identity is not None and current.file_identity != approved.file_identity
    ):
        raise PrimeSelectionError("config_changed")


def _private_write(path: Path, raw: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(raw)
            stream.flush()
        os.fsync(fd)
    finally:
        os.close(fd)


def _sync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_bytes(path: Path, raw: bytes, *, guard: Callable[[], None]) -> None:
    temp = path.with_name(f".{path.name}.verdict-{uuid.uuid4().hex}.tmp")
    try:
        _private_write(temp, raw)
        guard()
        os.replace(temp, path)
    finally:
        # A failed unlink can leave only a unique private temp, never a partial
        # settings file. Do not mask a durable settings rename.
        with suppress(OSError):
            temp.unlink(missing_ok=True)


def _receipt_name(backup: Path) -> Path:
    return backup.with_name(backup.name[:-4] + ".receipt.json")


def _prune(paths: SelectionPaths, family: str, current: Path) -> None:
    completed: list[Path] = []
    for candidate in paths.agent_dir.iterdir():
        match = _OWNED.fullmatch(candidate.name)
        if match is None or match[1] != family:
            continue
        try:
            raw, _ = _read(candidate, private=True)
            doc = _json(raw)
            backup = _validated_backup(paths, candidate, doc)
            if doc.get("status") == "applied":
                completed.append(backup)
        except (OSError, PrimeSelectionError):
            continue
    newest = sorted(completed, key=lambda p: p.name, reverse=True)
    keep = {current}
    for backup in newest:
        if len(keep) >= 5:
            break
        keep.add(backup)
    for backup in newest:
        if backup not in keep:
            try:
                _receipt_name(backup).unlink()
                backup.unlink()
            except OSError:
                pass


def _validated_backup(
    paths: SelectionPaths, receipt_path: Path, receipt: Mapping[str, Any]
) -> Path:
    match = _OWNED.fullmatch(receipt_path.name)
    if match is None or receipt_path.parent != paths.agent_dir:
        raise PrimeSelectionError("receipt_path_invalid")
    family = match[1]
    expected_schema = RECEIPT_SCHEMA if family == "select" else RESTORE_RECEIPT_SCHEMA
    backup = receipt_path.with_name(receipt_path.name[:-13] + ".bak")
    if (
        receipt.get("schema") != expected_schema
        or receipt.get("target") != str(paths.settings)
        or receipt.get("transaction_id") != match[3]
        or receipt.get("backup_name") != backup.name
        or receipt.get("status") not in {"prepared", "applied"}
    ):
        raise PrimeSelectionError("receipt_invalid")
    raw, _ = _read(backup, private=True)
    if byte_digest(raw) != receipt.get("backup_digest") or byte_digest(raw) != receipt.get(
        "pre_digest"
    ):
        raise PrimeSelectionError("backup_digest_mismatch")
    return backup


def load_receipt(
    paths: SelectionPaths, *, transaction_id: str | None = None
) -> tuple[Path, dict[str, Any], bytes]:
    """Load newest completed/recoverable selection, or one exact transaction id.

    Validates paths, ownership, schema and backup integrity. It never prints the
    full private preimage. Prepared records are usable only at their post digest.
    """
    if transaction_id is not None and re.fullmatch(r"[a-f0-9]{32}", transaction_id) is None:
        raise PrimeSelectionError("transaction_id_invalid")
    candidates = sorted(paths.agent_dir.iterdir(), key=lambda p: p.name, reverse=True)
    for path in candidates:
        match = _OWNED.fullmatch(path.name)
        if match is None or match[1] != "select" or (transaction_id and match[3] != transaction_id):
            continue
        raw, _ = _read(path, private=True)
        receipt = _json(raw)
        backup = _validated_backup(paths, path, receipt)
        current = load_settings(paths.settings)
        if current.pre_digest == receipt.get("pre_digest") and receipt.get("status") == "prepared":
            continue
        preimage, _ = _read(backup, private=True)
        return path, receipt, preimage
    raise PrimeSelectionError("selection_receipt_missing")


def _commit(
    current: SettingsDocument,
    payload: bytes,
    *,
    paths: SelectionPaths,
    family: str,
    clock: Callable[[], datetime],
    guard: Callable[[], None],
    extra: Mapping[str, Any] | None = None,
) -> SelectionResult:
    stamp = aware_time(clock()).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    txn = uuid.uuid4().hex
    backup = paths.settings.with_name(f"settings.json.verdict-{family}-{stamp}-{txn}.bak")
    receipt_path = _receipt_name(backup)
    post = byte_digest(payload)
    receipt: dict[str, Any] = {
        "schema": RECEIPT_SCHEMA if family == "select" else RESTORE_RECEIPT_SCHEMA,
        "target": str(paths.settings),
        "transaction_id": txn,
        "pre_digest": current.pre_digest,
        "post_digest": post,
        "backup_name": backup.name,
        "backup_digest": current.pre_digest,
        "timestamp": stamp,
        "before": list(_scope(current.value)),
        "after": list(_scope(_json(payload))),
        "status": "prepared",
        **(extra or {}),
    }
    replaced = False
    try:
        _private_write(backup, current.raw_bytes)
        _private_write(receipt_path, _encode(receipt))
        _sync_dir(paths.agent_dir)
        _atomic_bytes(paths.settings, payload, guard=guard)
        replaced = True
        _sync_dir(paths.agent_dir)
        receipt["status"] = "applied"
        _atomic_bytes(receipt_path, _encode(receipt), guard=lambda: None)
        _sync_dir(paths.agent_dir)
        _prune(paths, family, backup)
    except (OSError, ValueError) as exc:
        reason = (
            str(exc) if isinstance(exc, PrimeSelectionError) else "private_transaction_io_failed"
        )
        return SelectionResult(
            "applied_receipt_incomplete" if replaced else "refused",
            (reason, "explicit_digest_checked_restore_available") if replaced else (reason,),
            txn,
            backup if backup.exists() else None,
            receipt_path if receipt_path.exists() else None,
            current.pre_digest,
            post if replaced else None,
        )
    return SelectionResult(
        "applied" if family == "select" else "restored",
        (),
        txn,
        backup,
        receipt_path,
        current.pre_digest,
        post,
    )


def apply_selection(
    preview: SelectionPreview,
    *,
    expected_pre_digest: str,
    confirmed: bool = False,
    paths: SelectionPaths,
    reload_rows: Callable[[], Sequence[Mapping[str, Any]]],
    reload_dependencies: Callable[[], PrimeDependencies],
    clock: Callable[[], datetime],
) -> SelectionResult:
    """Revalidate local proof/config under flock and commit exactly one key.

    Reload callbacks MUST be local/read-only, without refresh, transport or wait.
    Expired original plan deadlines require a new preview, never an extension.
    A no-op still checks proof and dependencies, and creates no backup/receipt.
    """
    if confirmed is not True:
        return SelectionResult("cancelled", ("confirmation_required",))
    if expected_pre_digest != preview.pre_digest:
        return SelectionResult("refused", ("pre_digest_mismatch",))
    if preview.refusals:
        return SelectionResult("refused", preview.refusals)
    try:
        original = preview_selection(
            preview._settings,
            _json(preview._projection_bytes)["rows"],
            selected_ids=preview.selected_ids,
            dependencies=preview._dependencies,
            now=preview._generated_at,
        )
        if original.plan_digest != preview.plan_digest:
            raise PrimeSelectionError("plan_changed")
        if paths.settings != preview.target:
            raise PrimeSelectionError("effective_path_unrecognized")
        with _lock(paths):
            current = load_settings(paths.settings)
            _check_current(current, preview._settings)

            def guard() -> None:
                latest = load_settings(paths.settings)
                _check_current(latest, current)
                deps = reload_dependencies()
                _verify_dependencies(deps, paths)
                if tuple(deps.digests().items()) != preview.dependency_digests:
                    raise PrimeSelectionError("dependencies_changed")
                rows = reload_rows()
                now = aware_time(clock())
                if preview.proof_deadline is None or now >= preview.proof_deadline:
                    raise PrimeSelectionError("proof_deadline_elapsed_new_preview_required")
                fresh = preview_selection(
                    latest, rows, selected_ids=preview.selected_ids, dependencies=deps, now=now
                )
                if fresh.refusals:
                    raise PrimeSelectionError("proof_or_compatibility_changed")
                # New checked_at/deadline is allowed only inside the approved
                # original deadline; exact selection/diff/dependencies must match.
                if fresh.after != preview.after or fresh.before != preview.before:
                    raise PrimeSelectionError("plan_changed")
                # Reload callbacks can take time. Recheck actual documents and
                # the original deadline after them, immediately before rename.
                _check_current(load_settings(paths.settings), current)
                _verify_dependencies(deps, paths)
                if aware_time(clock()) >= preview.proof_deadline:
                    raise PrimeSelectionError("proof_deadline_elapsed_new_preview_required")

            guard()
            if _scope(current.value) == preview.after:
                return SelectionResult(
                    "unchanged", pre_digest=current.pre_digest, post_digest=current.pre_digest
                )
            changed = current.value
            changed["enabledModels"] = list(preview.after)
            return _commit(
                current, _encode(changed), paths=paths, family="select", clock=clock, guard=guard
            )
    except (OSError, ValueError) as exc:
        code = str(exc) if isinstance(exc, PrimeSelectionError) else "selection_validation_failed"
        return SelectionResult("busy" if code == "busy" else "refused", (code,))


@dataclass(frozen=True)
class RestorePreview:
    target: Path
    transaction_id: str
    expected_post_digest: str
    restored_digest: str
    before: tuple[str, ...]
    after: tuple[str, ...]
    refusals: tuple[str, ...]
    _settings: SettingsDocument = field(repr=False)
    _receipt_bytes: bytes = field(repr=False)
    _backup_bytes: bytes = field(repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "verdict.prime-restore/v1",
            "target": str(self.target),
            "transaction_id": self.transaction_id,
            "expected_post_digest": self.expected_post_digest,
            "restored_digest": self.restored_digest,
            "key": "/enabledModels",
            "before": list(self.before),
            "after": list(self.after),
            "refusals": list(self.refusals),
            "safe_full_preimage_restore": not self.refusals,
            "warning": "restored prior scope, not newly verified",
        }


def preview_restore(
    settings: SettingsDocument, receipt: Mapping[str, Any], *, backup_bytes: bytes
) -> RestorePreview:
    """Pure byte-integrity/digest guarded restore preview, with no health claim."""
    errors: list[str] = []
    txn = receipt.get("transaction_id")
    if not isinstance(txn, str) or re.fullmatch(r"[a-f0-9]{32}", txn) is None:
        raise PrimeSelectionError("receipt_invalid")
    if (
        receipt.get("schema") != RECEIPT_SCHEMA
        or receipt.get("target") != str(settings.path)
        or receipt.get("status") not in {"prepared", "applied"}
    ):
        errors.append("receipt_invalid")
    post = receipt.get("post_digest")
    if not isinstance(post, str) or re.fullmatch(r"[a-f0-9]{64}", post) is None:
        raise PrimeSelectionError("receipt_invalid")
    if settings.pre_digest != post:
        errors.append("config_changed")
    if byte_digest(backup_bytes) != receipt.get("backup_digest") or byte_digest(
        backup_bytes
    ) != receipt.get("pre_digest"):
        errors.append("backup_digest_mismatch")
    before = _scope(settings.value)
    after = _scope(_json(backup_bytes))
    if any(not exact_token(t) for t in (*before, *after)):
        errors.append("unsafe_scope_token")
    return RestorePreview(
        settings.path,
        txn,
        post,
        byte_digest(backup_bytes),
        tuple(t if exact_token(t) else "[unsafe entry]" for t in before),
        tuple(t if exact_token(t) else "[unsafe entry]" for t in after),
        tuple(errors),
        settings,
        _encode(receipt),
        backup_bytes,
    )


def _already_restored(
    paths: SelectionPaths, preview: RestorePreview, current: SettingsDocument
) -> bool:
    for path in paths.agent_dir.iterdir():
        match = _OWNED.fullmatch(path.name)
        if match is None or match[1] != "restore":
            continue
        try:
            raw, _ = _read(path, private=True)
            doc = _json(raw)
            _validated_backup(paths, path, doc)
        except (OSError, PrimeSelectionError):
            continue
        if (
            doc.get("status") == "applied"
            and doc.get("source_transaction") == preview.transaction_id
            and doc.get("source_post_digest") == preview.expected_post_digest
            and doc.get("post_digest") == current.pre_digest == preview.restored_digest
        ):
            return True
    return False


def restore_selection(
    preview: RestorePreview,
    *,
    expected_post_digest: str,
    confirmed: bool = False,
    paths: SelectionPaths,
    clock: Callable[[], datetime],
    reload_dependencies: Callable[[], PrimeDependencies],
) -> SelectionResult:
    """Restore validated bytes only at the applied digest; no force option.

    Discovery must still be safe/installed. Historical health is not checked.
    Recorded repeated restore is unchanged only at its exact restored digest.
    """
    if confirmed is not True:
        return SelectionResult("cancelled", ("confirmation_required",))
    if expected_post_digest != preview.expected_post_digest:
        return SelectionResult("refused", ("post_digest_mismatch",))
    try:
        original = preview_restore(
            preview._settings, _json(preview._receipt_bytes), backup_bytes=preview._backup_bytes
        )
        if original.to_dict() != preview.to_dict():
            raise PrimeSelectionError("plan_changed")
        if paths.settings != preview.target:
            raise PrimeSelectionError("effective_path_unrecognized")
        with _lock(paths):
            _verify_dependencies(reload_dependencies(), paths)
            current = load_settings(paths.settings)
            _, receipt, backup = load_receipt(paths, transaction_id=preview.transaction_id)
            if _encode(receipt) != preview._receipt_bytes or backup != preview._backup_bytes:
                raise PrimeSelectionError("recovery_dependency_changed")
            if _already_restored(paths, preview, current):
                return SelectionResult(
                    "unchanged",
                    ("restored prior scope, not newly verified",),
                    pre_digest=current.pre_digest,
                    post_digest=current.pre_digest,
                )
            if preview.refusals:
                return SelectionResult("refused", preview.refusals)
            if current.pre_digest != expected_post_digest:
                raise PrimeSelectionError("config_changed")
            _check_current(current, preview._settings)

            def guard() -> None:
                _check_current(load_settings(paths.settings), current)
                _verify_dependencies(reload_dependencies(), paths)
                _, latest_receipt, latest_backup = load_receipt(
                    paths, transaction_id=preview.transaction_id
                )
                if _encode(latest_receipt) != preview._receipt_bytes or latest_backup != backup:
                    raise PrimeSelectionError("recovery_dependency_changed")
                _check_current(load_settings(paths.settings), current)

            return _commit(
                current,
                backup,
                paths=paths,
                family="restore",
                clock=clock,
                guard=guard,
                extra={
                    "source_transaction": preview.transaction_id,
                    "source_post_digest": expected_post_digest,
                    "warning": "restored prior scope, not newly verified",
                },
            )
    except (OSError, ValueError) as exc:
        code = str(exc) if isinstance(exc, PrimeSelectionError) else "restore_validation_failed"
        return SelectionResult("busy" if code == "busy" else "refused", (code,))
