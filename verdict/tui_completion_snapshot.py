"""Disposable sanitized completion cache. All I/O occurs outside keystrokes.

Capture one local evidence generation, then project bounded pages from that same
input. Publication is convenience only, never health/visibility/admission proof.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from verdict.actions.verified_models import VerifiedSnapshotAdapter, _runtime_negatives
from verdict.admission import active_controller_route, admit
from verdict.orchestration.verified_models import (
    VerifiedModelQuery,
    project_verified_models,
    snapshots_from_documents,
)
from verdict.tui_completion import SCHEMA, CompletionSnapshot

MAX_BYTES = 4 * 1024 * 1024
MAX_ROWS = 10_000
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+@-]{0,255}\Z")
_SECRET = re.compile(r"(?i)(sk-|bearer|api[_-]?key|token[=:]|https?://)")
_STATUSES = {
    "VERIFIED",
    "STALE",
    "FAILED",
    "UNAVAILABLE",
    "UNVERIFIED",
    "INVENTORY_ONLY",
    "EXCLUDED",
}
_FIELDS = (
    "route_id",
    "provider",
    "status",
    "checked_at",
    "fresh_until",
    "expires_at",
    "identity",
    "coding_ok",
)


def snapshot_path() -> Path:
    return (
        Path(os.environ.get("VERDICT_HOME", Path.home() / ".verdict"))
        / "verified-completion-snapshot.json"
    )


def _id(value: Any) -> bool:
    return isinstance(value, str) and bool(_ID.fullmatch(value)) and not _SECRET.search(value)


def _stamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return stamp if stamp.tzinfo is not None else None
    except ValueError:
        return None


def sanitize_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        rid, provider = row.get("route_id"), row.get("provider")
        if not _id(rid) or not _id(provider):
            continue
        result[str(rid)] = {
            "route_id": rid,
            "provider": provider,
            "status": row.get("status") if row.get("status") in _STATUSES else "UNVERIFIED",
            "identity": row.get("identity")
            if row.get("identity") in {"verified", "unknown", "unverified"}
            else None,
            "coding_ok": row.get("coding_ok") is True,
            **{
                key: stamp.isoformat() if (stamp := _stamp(row.get(key))) else None
                for key in ("checked_at", "fresh_until", "expires_at")
            },
        }
    return [result[rid] for rid in sorted(result)]


def local_projection(adapter: VerifiedSnapshotAdapter, *, now: datetime) -> dict[str, Any]:
    """Read each local input once. Never call metadata loaders or live adapters."""
    errors: list[str] = []

    def read(path: Path) -> Mapping[str, Any] | None:
        try:
            value = json.loads(path.read_bytes())
            if isinstance(value, Mapping):
                return value
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            pass
        errors.append("local_source_unavailable")
        return None

    catalog = read(adapter.paths.catalog) if adapter.inventory_rows is None else {}
    inventory = (
        adapter.inventory_rows
        if adapter.inventory_rows is not None
        else (catalog or {}).get("inventory_rows", (catalog or {}).get("data", []))
    )
    connections = (
        adapter.connections
        if adapter.connections is not None
        else (catalog or {}).get("connections")
    )
    inventory = (
        [row for row in inventory if isinstance(row, Mapping)]
        if isinstance(inventory, Sequence)
        else []
    )
    docs: dict[str, Any] = {
        "health_cache_doc": read(adapter.paths.health_cache),
        "ladder_state_doc": read(adapter.paths.ladder),
        "worker_health_doc": read(adapter.paths.worker),
        "admission_receipt_doc": read(adapter.paths.receipt),
    }
    evidence = snapshots_from_documents(**docs, now=now, extra_source_errors=errors)
    facts = None
    if isinstance(connections, list):
        facts = {
            record.route_id: record.to_dict()
            for record in admit(
                inventory,
                connections,
                _runtime_negatives(evidence, now),
                now=now,
                require_runtime=False,
            ).records
        }
    exclusions = dict(adapter.policy_exclusions or {})
    controller = active_controller_route()
    if controller:
        exclusions[controller] = "active_controller"
    evidence = snapshots_from_documents(
        **docs,
        now=now,
        admission_facts=facts,
        policy_exclusions=exclusions,
        extra_source_errors=errors,
    )
    first = project_verified_models(
        inventory, connections, evidence, now=now, query=VerifiedModelQuery(page_size=200)
    )
    rows = list(first.to_dict()["rows"])
    for page in range(2, min(first.page_count, 50) + 1):
        rows.extend(
            project_verified_models(
                inventory,
                connections,
                evidence,
                now=now,
                query=VerifiedModelQuery(page=page, page_size=200),
            ).to_dict()["rows"]
        )
    return {
        "generated_at": now.isoformat(),
        "rows": rows,
        "source_errors": first.source_errors,
        "incomplete": first.total_count > MAX_ROWS,
    }


def publish_snapshot(view: Mapping[str, Any], path: Path | None = None) -> str | None:
    """Write only approved metadata at 0600; return a safe warning on failure."""
    path = path or snapshot_path()
    rows = sanitize_rows(view.get("rows", []))
    incomplete = bool(view.get("incomplete")) or len(rows) > MAX_ROWS
    rows = rows[:MAX_ROWS]
    errors = ["local_source_unavailable"] if view.get("source_errors") else []
    if incomplete:
        errors.append("snapshot_incomplete")
    payload = {
        "schema": SCHEMA,
        "generated_at": (
            stamp.isoformat() if (stamp := _stamp(view.get("generated_at"))) else None
        ),
        "model_rows": rows,
        "provider_names": sorted({row["provider"] for row in rows}),
        "source_errors": errors,
    }

    def encode() -> bytes:
        return (json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n").encode()

    raw = encode()
    while len(raw) > MAX_BYTES and rows:
        rows = rows[: len(rows) * 3 // 4]
        payload["model_rows"] = rows
        payload["provider_names"] = sorted({row["provider"] for row in rows})
        payload["source_errors"] = sorted(set([*errors, "snapshot_incomplete"]))
        raw = encode()
    temp: str | None = None
    try:
        if _stamp(payload["generated_at"]) is None or len(raw) > MAX_BYTES:
            raise ValueError("invalid_snapshot")
        if any(parent.is_symlink() for parent in (path, *path.parents)):
            raise ValueError("unsafe_path")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = path.parent.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise ValueError("unsafe_directory")
        fd, temp = tempfile.mkstemp(prefix=".verified-completion-", dir=path.parent)
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        return (
            "completion snapshot incomplete; suggestions suppressed"
            if payload["source_errors"]
            else None
        )
    except (ValueError, OSError, TypeError):
        return "completion snapshot publication failed; evidence unchanged"
    finally:
        if temp is not None:
            with contextlib.suppress(OSError):
                Path(temp).unlink(missing_ok=True)


def load_snapshot(
    path: Path | None = None, *, now: datetime, runs: Sequence[Mapping[str, Any]] = ()
) -> CompletionSnapshot:
    """Load once on prompt entry/post-command. Invalid files have no model rows."""
    safe_runs = [
        {"run": row.get("run"), "outcome": str(row.get("outcome", ""))[:64]}
        for row in runs[:200]
        if _id(row.get("run"))
    ]

    def build(
        rows: Sequence[Mapping[str, Any]], generated: str, errors: Sequence[str]
    ) -> CompletionSnapshot:
        return CompletionSnapshot(
            SCHEMA,
            generated,
            safe_runs,
            rows,
            sorted({str(row["provider"]) for row in rows}),
            ("prime", "claude"),
            ("native", "openai-side-path"),
            errors,
        )

    try:
        path = path or snapshot_path()
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_size > MAX_BYTES
            ):
                raise ValueError("unsafe_snapshot")
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("oversize_snapshot")
        value = json.loads(raw)
        generated = _stamp(value.get("generated_at"))
        if value.get("schema") != SCHEMA or generated is None or generated > now:
            raise ValueError("invalid_snapshot")
        rows = value.get("model_rows")
        if (
            not isinstance(rows, list)
            or len(rows) > MAX_ROWS
            or any(not isinstance(row, Mapping) for row in rows)
        ):
            raise ValueError("invalid_rows")
        safe = sanitize_rows(rows)
        if len(safe) != len(rows) or any(set(row) - set(_FIELDS) for row in rows):
            raise ValueError("unsafe_rows")
        errors = ("snapshot_source_error",) if value.get("source_errors") else ()
        return build(safe, str(value["generated_at"]), errors)
    except (ValueError, OSError, TypeError, AttributeError):
        return build((), now.isoformat(), ("snapshot_unavailable",))
