"""Portable, source-bound continuation packets for one bounded work unit.

The packet stores coordination metadata and evidence references, never raw
conversation or provider payloads.  It composes existing source, checkpoint,
context, and receipt authorities rather than replacing them.
"""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any, cast

from verdict.receipt_store import ReceiptRecord, ReceiptStore

EXECUTION_PACKET_SCHEMA_VERSION = "1"
SUPPORTED_SCHEMA_VERSIONS = frozenset({EXECUTION_PACKET_SCHEMA_VERSION})
_DIGEST_PREFIX = "sha256:"
_FORBIDDEN_KEYS = frozenset(
    {
        "api_key",
        "authorization",
        "completion",
        "credential",
        "messages",
        "password",
        "prompt",
        "secret",
        "token",
        "tool_arguments",
    }
)
_TASK_STATES = frozenset({"pending", "active", "blocked", "completed", "failed", "uncertain"})
_SIDE_EFFECT_KINDS = frozenset({"read-only", "reversible", "irreversible"})


class ExecutionPacketError(ValueError):
    """Raised for malformed packets, drift, or unsafe resume state."""


class UnsupportedSchemaVersionError(ExecutionPacketError):
    """A packet carries a schema_version this worker does not support.

    Raised before any inference or gateway activity; the refusal is
    observable through :func:`schema_refusal_receipt`.
    """

    def __init__(self, encountered: str, supported: frozenset[str] | set[str]) -> None:
        self.encountered = encountered
        self.supported = sorted(supported)
        super().__init__(
            f"unsupported schema_version: encountered {encountered!r}, "
            f"supported {sorted(supported)!r}; refusing before any gateway request"
        )


def schema_refusal_receipt(error: UnsupportedSchemaVersionError) -> dict[str, Any]:
    """Machine-readable pre-gateway refusal receipt for an unsupported version."""
    return {
        "refusal": "unsupported-schema-version",
        "encountered_schema_version": error.encountered,
        "supported_schema_versions": error.supported,
        "gateway_requests_issued": 0,
    }


class ProofLevel(str, Enum):
    LIVE_PROVEN = "live-proven"
    SOURCE_ONLY = "source-only"
    FIXTURE_ONLY = "fixture-only"
    PARTIAL = "partial"
    MISSING = "missing"
    UNKNOWN = "unknown"


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    return _DIGEST_PREFIX + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _plain(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


def _strict(
    value: Mapping[str, Any], *, required: set[str], optional: set[str], field_name: str
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ExecutionPacketError(f"{field_name} must be an object")
    unknown = sorted(set(value) - required - optional)
    if unknown:
        raise ExecutionPacketError(f"{field_name} has unknown field(s): {unknown}")
    missing = sorted(required - set(value))
    if missing:
        raise ExecutionPacketError(f"{field_name} missing field(s): {missing}")
    return dict(value)


def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExecutionPacketError(f"{field_name} must be a non-empty string")
    return value.strip()


def _integer(value: Any, field_name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ExecutionPacketError(f"{field_name} must be an integer >= {minimum}")
    return value


def _number(value: Any, field_name: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ExecutionPacketError(f"{field_name} must be a number")
    result = float(value)
    if not math.isfinite(result) or result < minimum:
        raise ExecutionPacketError(f"{field_name} must be finite and >= {minimum}")
    return result


def _strings(value: Any, field_name: str, *, allow_empty: bool = True) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ExecutionPacketError(f"{field_name} must be a list of strings")
    items = tuple(_text(item, field_name) for item in value)
    if not allow_empty and not items:
        raise ExecutionPacketError(f"{field_name} must not be empty")
    if len(set(items)) != len(items):
        raise ExecutionPacketError(f"{field_name} contains duplicate values")
    return items


def _digest(value: Any, field_name: str) -> str:
    text = _text(value, field_name)
    suffix = text.removeprefix(_DIGEST_PREFIX)
    if not text.startswith(_DIGEST_PREFIX) or len(suffix) != 64:
        raise ExecutionPacketError(f"{field_name} must be a sha256 digest")
    try:
        int(suffix, 16)
    except ValueError as exc:
        raise ExecutionPacketError(f"{field_name} must be a sha256 digest") from exc
    return text


def _reject_forbidden(value: Any, path: str = "packet") -> None:
    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            key = str(raw_key).lower()
            if key in _FORBIDDEN_KEYS:
                raise ExecutionPacketError(f"forbidden field at {path}.{raw_key}")
            _reject_forbidden(item, f"{path}.{raw_key}")
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for index, item in enumerate(value):
            _reject_forbidden(item, f"{path}[{index}]")


def _path(value: Any, field_name: str) -> str:
    text = _text(value, field_name)
    candidate = PurePosixPath(text)
    if candidate.is_absolute() or ".." in candidate.parts or "\\" in text:
        raise ExecutionPacketError(f"{field_name} must be a safe repository-relative path")
    return str(candidate)


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _freeze(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return tuple(_freeze(item) for item in value)
    return value


def _git(repo: Path, *args: str, binary: bool = False) -> bytes | str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=not binary,
        timeout=15,
    )
    return cast(bytes | str, result.stdout)


def capture_worktree_digest(repo: str | Path) -> str:
    """Digest exact tracked/index/untracked content without storing its bytes."""

    root = Path(repo).expanduser().resolve()
    status_raw = cast(
        bytes, _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all", binary=True)
    )
    records: list[dict[str, str]] = []
    for entry in status_raw.split(b"\0"):
        if not entry:
            continue
        decoded = entry.decode("utf-8", errors="surrogateescape")
        status = decoded[:2]
        path_text = decoded[3:]
        path = root / path_text
        content_digest = (
            _DIGEST_PREFIX
            + hashlib.sha256(path.read_bytes() if path.is_file() else b"").hexdigest()
        )
        records.append({"status": status, "path": path_text, "content_digest": content_digest})
    index_tree = cast(str, _git(root, "write-tree")).strip()
    return _sha256({"index_tree": index_tree, "records": records})


def capture_source_binding(
    repo: str | Path,
    *,
    repository: str | None = None,
    lock_paths: Sequence[str] = ("uv.lock", "package-lock.json"),
) -> Mapping[str, Any]:
    """Capture the packet's exact source identity using local Git and file reads."""

    root = Path(repo).expanduser().resolve()
    repository_value = repository
    if repository_value is None:
        try:
            repository_value = cast(str, _git(root, "remote", "get-url", "origin")).strip()
        except subprocess.CalledProcessError:
            repository_value = str(root)
    lock_digests: dict[str, str] = {}
    for raw_path in lock_paths:
        normalized = _path(raw_path, "lock_path")
        path = root / normalized
        if path.is_file():
            lock_digests[normalized] = (
                _DIGEST_PREFIX + hashlib.sha256(path.read_bytes()).hexdigest()
            )
    return {
        "repository": _text(repository_value, "repository"),
        "worktree": str(root),
        "commit": cast(str, _git(root, "rev-parse", "HEAD")).strip(),
        "branch": cast(str, _git(root, "branch", "--show-current")).strip(),
        "dirty_digest": capture_worktree_digest(root),
        "lock_digests": lock_digests,
    }


@dataclass(frozen=True)
class PacketTransition:
    transition_id: str
    task_id: str
    from_state: str
    to_state: str
    reason: str
    evidence_refs: tuple[str, ...] = ()
    side_effect_kind: str = "read-only"
    committed: bool = False

    def __post_init__(self) -> None:
        for name in ("transition_id", "task_id", "reason"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        if self.from_state not in _TASK_STATES or self.to_state not in _TASK_STATES:
            raise ExecutionPacketError("transition state is unsupported")
        if self.side_effect_kind not in _SIDE_EFFECT_KINDS:
            raise ExecutionPacketError("side_effect_kind is unsupported")
        if not isinstance(self.committed, bool):
            raise ExecutionPacketError("committed must be a boolean")
        object.__setattr__(
            self, "evidence_refs", _strings(self.evidence_refs, "evidence_refs", allow_empty=True)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "transition_id": self.transition_id,
            "task_id": self.task_id,
            "from_state": self.from_state,
            "to_state": self.to_state,
            "reason": self.reason,
            "evidence_refs": list(self.evidence_refs),
            "side_effect_kind": self.side_effect_kind,
            "committed": self.committed,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> PacketTransition:
        payload = _strict(
            value,
            required={"transition_id", "task_id", "from_state", "to_state", "reason"},
            optional={"evidence_refs", "side_effect_kind", "committed"},
            field_name="transition",
        )
        return cls(
            transition_id=payload["transition_id"],
            task_id=payload["task_id"],
            from_state=payload["from_state"],
            to_state=payload["to_state"],
            reason=payload["reason"],
            evidence_refs=tuple(payload.get("evidence_refs", ())),
            side_effect_kind=payload.get("side_effect_kind", "read-only"),
            committed=payload.get("committed", False),
        )


@dataclass(frozen=True)
class ExecutionPacket:
    packet_id: str
    packet_version: int
    story_id: str
    story_version: str
    source: Mapping[str, Any]
    intent: Mapping[str, Any]
    authority: Mapping[str, Any]
    verification: Mapping[str, Any]
    decisions: tuple[Mapping[str, Any], ...] = ()
    context_refs: tuple[Mapping[str, Any], ...] = ()
    tasks: tuple[Mapping[str, Any], ...] = ()
    route_attempts: tuple[Mapping[str, Any], ...] = ()
    failure_history: tuple[Mapping[str, Any], ...] = ()
    transitions: tuple[PacketTransition, ...] = ()
    checkpoint_refs: tuple[str, ...] = ()
    receipt_refs: tuple[str, ...] = ()
    next_safe_action: str = ""
    proof_level: ProofLevel = ProofLevel.UNKNOWN
    parent_integrity_digest: str | None = None
    executing_model: str | None = field(default=None, compare=False)
    schema_version: str = EXECUTION_PACKET_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version not in SUPPORTED_SCHEMA_VERSIONS:
            raise UnsupportedSchemaVersionError(
                self.schema_version, SUPPORTED_SCHEMA_VERSIONS
            )
        for name in ("packet_id", "story_id", "story_version", "next_safe_action"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        object.__setattr__(
            self, "packet_version", _integer(self.packet_version, "packet_version", minimum=1)
        )
        object.__setattr__(self, "proof_level", ProofLevel(self.proof_level))
        if self.parent_integrity_digest is not None:
            object.__setattr__(
                self,
                "parent_integrity_digest",
                _digest(self.parent_integrity_digest, "parent_integrity_digest"),
            )
        if self.executing_model is not None:
            object.__setattr__(
                self, "executing_model", _text(self.executing_model, "executing_model")
            )
        normalized = _normalize_sections(
            source=self.source,
            intent=self.intent,
            authority=self.authority,
            verification=self.verification,
            decisions=self.decisions,
            context_refs=self.context_refs,
            tasks=self.tasks,
            route_attempts=self.route_attempts,
            failure_history=self.failure_history,
        )
        for name, value in normalized.items():
            object.__setattr__(self, name, value)
        object.__setattr__(
            self,
            "transitions",
            tuple(
                item if isinstance(item, PacketTransition) else PacketTransition.from_dict(item)
                for item in self.transitions
            ),
        )
        object.__setattr__(
            self,
            "checkpoint_refs",
            _strings(self.checkpoint_refs, "checkpoint_refs", allow_empty=True),
        )
        object.__setattr__(
            self, "receipt_refs", _strings(self.receipt_refs, "receipt_refs", allow_empty=True)
        )
        _reject_forbidden(self._integrity_payload())

    @property
    def integrity_digest(self) -> str:
        return _sha256(self._integrity_payload())

    @property
    def immutable_digest(self) -> str:
        return _sha256(self._immutable_payload())

    def _immutable_payload(self) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            _plain(
                {
                    "story_id": self.story_id,
                    "story_version": self.story_version,
                    "source": self.source,
                    "intent": self.intent,
                    "authority": self.authority,
                    "verification": self.verification,
                    "decisions": self.decisions,
                    "context_refs": self.context_refs,
                }
            ),
        )

    def _integrity_payload(self) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            _plain(
                {
                    "schema_version": self.schema_version,
                    "packet_id": self.packet_id,
                    "packet_version": self.packet_version,
                    "story_id": self.story_id,
                    "story_version": self.story_version,
                    "source": self.source,
                    "intent": self.intent,
                    "authority": self.authority,
                    "verification": self.verification,
                    "decisions": self.decisions,
                    "context_refs": self.context_refs,
                    "tasks": self.tasks,
                    "route_attempts": self.route_attempts,
                    "failure_history": self.failure_history,
                    "transitions": tuple(item.to_dict() for item in self.transitions),
                    "checkpoint_refs": self.checkpoint_refs,
                    "receipt_refs": self.receipt_refs,
                    "next_safe_action": self.next_safe_action,
                    "proof_level": self.proof_level,
                    "parent_integrity_digest": self.parent_integrity_digest,
                }
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {**self._integrity_payload(), "integrity_digest": self.integrity_digest}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ExecutionPacket:
        _reject_forbidden(value)
        payload = _strict(
            value,
            required={
                "schema_version",
                "packet_id",
                "packet_version",
                "story_id",
                "story_version",
                "source",
                "intent",
                "authority",
                "verification",
                "decisions",
                "context_refs",
                "tasks",
                "route_attempts",
                "failure_history",
                "transitions",
                "checkpoint_refs",
                "receipt_refs",
                "next_safe_action",
                "proof_level",
            },
            optional={"parent_integrity_digest", "executing_model", "integrity_digest"},
            field_name="execution_packet",
        )
        expected = payload.pop("integrity_digest", None)
        packet = cls(
            packet_id=payload["packet_id"],
            packet_version=payload["packet_version"],
            story_id=payload["story_id"],
            story_version=payload["story_version"],
            source=payload["source"],
            intent=payload["intent"],
            authority=payload["authority"],
            verification=payload["verification"],
            decisions=tuple(payload["decisions"]),
            context_refs=tuple(payload["context_refs"]),
            tasks=tuple(payload["tasks"]),
            route_attempts=tuple(payload["route_attempts"]),
            failure_history=tuple(payload["failure_history"]),
            transitions=tuple(payload["transitions"]),
            checkpoint_refs=tuple(payload["checkpoint_refs"]),
            receipt_refs=tuple(payload["receipt_refs"]),
            next_safe_action=payload["next_safe_action"],
            proof_level=payload["proof_level"],
            parent_integrity_digest=payload.get("parent_integrity_digest"),
            executing_model=payload.get("executing_model"),
            schema_version=payload["schema_version"],
        )
        if (
            expected is not None
            and _digest(expected, "integrity_digest") != packet.integrity_digest
        ):
            raise ExecutionPacketError("integrity digest mismatch")
        return packet

    def for_model(self, model_id: str) -> ExecutionPacket:
        return replace(self, executing_model=_text(model_id, "model_id"))

    def persist(self, store: ReceiptStore, *, scope: str = "operational-loop") -> ReceiptRecord:
        """Persist the secret-free packet as an idempotent manifest receipt."""

        return store.put_receipt(
            "manifest",
            scope,
            self.to_dict(),
            provenance={"source": "verdict.execution_packet", "authority": "local-observation"},
            idempotency_key=f"packet:{self.integrity_digest}",
        )

    def persist_transition(
        self, store: ReceiptStore, item: PacketTransition, *, scope: str = "operational-loop"
    ) -> tuple[ExecutionPacket, ReceiptRecord]:
        """Append one transition receipt and return a packet referencing it."""

        parent = self.persist(store, scope=scope)
        advanced = self.transition(item)
        event = store.put_receipt(
            "execution",
            scope,
            {
                "packet_id": self.packet_id,
                "packet_version": advanced.packet_version,
                "parent_integrity_digest": self.integrity_digest,
                "integrity_digest": advanced.integrity_digest,
                "transition": item.to_dict(),
            },
            provenance={"source": "verdict.execution_packet", "authority": "observed"},
            parent_receipt_id=parent.receipt_id,
            event_id=item.transition_id,
            event_type="execution_packet.transition",
            idempotency_key=f"transition:{self.packet_id}:{item.transition_id}",
        )
        return replace(advanced, receipt_refs=(*advanced.receipt_refs, event.receipt_id)), event

    def validate_resume(self, current: ExecutionPacket) -> None:
        if self.immutable_digest != current.immutable_digest:
            raise ExecutionPacketError("immutable packet drift; create a new packet version")
        for transition in current.transitions:
            if transition.committed and transition.to_state == "uncertain":
                raise ExecutionPacketError(
                    "uncertain committed write requires side-effect recovery"
                )

    def transition(self, item: PacketTransition) -> ExecutionPacket:
        existing = next(
            (
                candidate
                for candidate in self.transitions
                if candidate.transition_id == item.transition_id
            ),
            None,
        )
        if existing is not None:
            if existing == item:
                return self
            raise ExecutionPacketError("conflicting duplicate transition")
        tasks = [dict(task) for task in self.tasks]
        target = next((task for task in tasks if task["task_id"] == item.task_id), None)
        if target is None:
            raise ExecutionPacketError(f"transition references unknown task {item.task_id!r}")
        if target["status"] != item.from_state:
            raise ExecutionPacketError("transition from_state does not match task state")
        target["status"] = item.to_state
        return replace(
            self,
            packet_version=self.packet_version + 1,
            parent_integrity_digest=self.integrity_digest,
            tasks=tuple(tasks),
            transitions=(*self.transitions, item),
        )

    def revise(self, *, reason: str, **changes: Any) -> ExecutionPacket:
        _text(reason, "reason")
        allowed = {"source", "intent", "authority", "verification", "decisions", "context_refs"}
        unknown = sorted(set(changes) - allowed)
        if unknown:
            raise ExecutionPacketError(f"unsupported revision field(s): {unknown}")
        return replace(
            self,
            **changes,
            packet_version=self.packet_version + 1,
            parent_integrity_digest=self.integrity_digest,
        )


class ExecutionPacketStore:
    """Atomic local JSON persistence for portable packet handoff."""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory).expanduser().resolve()

    def _path(self, packet_or_path: ExecutionPacket | str | Path) -> Path:
        if isinstance(packet_or_path, ExecutionPacket):
            return self.directory / f"{packet_or_path.packet_id}.json"
        path = Path(packet_or_path).expanduser()
        if not path.is_absolute():
            path = self.directory / path
        return path.resolve()

    @staticmethod
    def _write(path: Path, packet: ExecutionPacket) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(
            json.dumps(packet.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)

    def create(self, packet: ExecutionPacket, path: str | Path | None = None) -> Path:
        path = self._path(packet if path is None else path)
        if path.exists():
            raise ExecutionPacketError(f"execution packet already exists: {path}")
        self._write(path, packet)
        return path

    def inspect(self, path: str | Path) -> ExecutionPacket:
        resolved = self._path(path)
        try:
            payload = json.loads(resolved.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ExecutionPacketError(f"execution packet not found: {resolved}") from exc
        except (OSError, ValueError) as exc:
            raise ExecutionPacketError(f"execution packet is not valid JSON: {resolved}") from exc
        if not isinstance(payload, Mapping):
            raise ExecutionPacketError("execution packet JSON must be an object")
        return ExecutionPacket.from_dict(payload)

    def validate(self, path: str | Path) -> ExecutionPacket:
        return self.inspect(path)

    def transition(self, path: str | Path, item: PacketTransition) -> ExecutionPacket:
        resolved = self._path(path)
        packet = self.validate(resolved)
        advanced = packet.transition(item)
        self._write(resolved, advanced)
        return advanced

    def resume(self, path: str | Path, *, executing_model: str) -> ExecutionPacket:
        packet = self.validate(path)
        packet.validate_resume(packet)
        return packet.for_model(executing_model)


def _normalize_sections(
    *,
    source: Mapping[str, Any],
    intent: Mapping[str, Any],
    authority: Mapping[str, Any],
    verification: Mapping[str, Any],
    decisions: Sequence[Mapping[str, Any]],
    context_refs: Sequence[Mapping[str, Any]],
    tasks: Sequence[Mapping[str, Any]],
    route_attempts: Sequence[Mapping[str, Any]],
    failure_history: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    source_value = _strict(
        source,
        required={"repository", "worktree", "commit", "branch", "dirty_digest", "lock_digests"},
        optional=set(),
        field_name="source",
    )
    for name in ("repository", "worktree", "commit", "branch"):
        source_value[name] = _text(source_value[name], f"source.{name}")
    source_value["dirty_digest"] = _digest(source_value["dirty_digest"], "source.dirty_digest")
    if not isinstance(source_value["lock_digests"], Mapping):
        raise ExecutionPacketError("source.lock_digests must be an object")
    source_value["lock_digests"] = {
        _path(key, "source.lock_digests key"): _digest(value, "source.lock_digests value")
        for key, value in source_value["lock_digests"].items()
    }

    intent_value = _strict(
        intent,
        required={"goal", "non_goals", "acceptance", "limitations"},
        optional=set(),
        field_name="intent",
    )
    intent_value["goal"] = _text(intent_value["goal"], "intent.goal")
    intent_value["non_goals"] = _strings(intent_value["non_goals"], "intent.non_goals")
    intent_value["acceptance"] = _strings(
        intent_value["acceptance"], "intent.acceptance", allow_empty=False
    )
    intent_value["limitations"] = _strings(intent_value["limitations"], "intent.limitations")

    authority_value = _strict(
        authority,
        required={
            "owned_paths",
            "denied_paths",
            "tools",
            "network",
            "max_spend_usd",
            "max_concurrency",
            "max_attempts",
            "destructive",
            "production",
        },
        optional=set(),
        field_name="authority",
    )
    authority_value["owned_paths"] = tuple(
        _path(item, "authority.owned_paths")
        for item in _strings(
            authority_value["owned_paths"], "authority.owned_paths", allow_empty=False
        )
    )
    authority_value["denied_paths"] = tuple(
        _path(item, "authority.denied_paths")
        for item in _strings(authority_value["denied_paths"], "authority.denied_paths")
    )
    authority_value["tools"] = _strings(authority_value["tools"], "authority.tools")
    for name in ("network", "destructive", "production"):
        if not isinstance(authority_value[name], bool):
            raise ExecutionPacketError(f"authority.{name} must be a boolean")
    authority_value["max_spend_usd"] = _number(
        authority_value["max_spend_usd"], "authority.max_spend_usd"
    )
    authority_value["max_concurrency"] = _integer(
        authority_value["max_concurrency"], "authority.max_concurrency", minimum=1
    )
    authority_value["max_attempts"] = _integer(
        authority_value["max_attempts"], "authority.max_attempts", minimum=1
    )

    verification_value = _strict(
        verification,
        required={"argv", "timeout_seconds"},
        optional=set(),
        field_name="verification",
    )
    verification_value["argv"] = _strings(
        verification_value["argv"], "verification.argv", allow_empty=False
    )
    verification_value["timeout_seconds"] = _integer(
        verification_value["timeout_seconds"], "verification.timeout_seconds", minimum=1
    )

    decision_values: list[Mapping[str, Any]] = []
    for index, item in enumerate(decisions):
        value = _strict(
            item, required={"ref", "digest"}, optional=set(), field_name=f"decisions[{index}]"
        )
        value["ref"] = _text(value["ref"], "decision.ref")
        value["digest"] = _digest(value["digest"], "decision.digest")
        decision_values.append(_freeze(value))

    context_values: list[Mapping[str, Any]] = []
    for index, item in enumerate(context_refs):
        value = _strict(
            item,
            required={"ref", "digest"},
            optional={"proof_level"},
            field_name=f"context_refs[{index}]",
        )
        value["ref"] = _text(value["ref"], "context_ref.ref")
        value["digest"] = _digest(value["digest"], "context_ref.digest")
        value["proof_level"] = ProofLevel(value.get("proof_level", ProofLevel.UNKNOWN))
        context_values.append(_freeze(value))

    task_values: list[Mapping[str, Any]] = []
    task_ids: set[str] = set()
    for index, item in enumerate(tasks):
        value = _strict(
            item,
            required={"task_id", "description", "status", "dependencies"},
            optional=set(),
            field_name=f"tasks[{index}]",
        )
        value["task_id"] = _text(value["task_id"], "task.task_id")
        value["description"] = _text(value["description"], "task.description")
        if value["task_id"] in task_ids:
            raise ExecutionPacketError("duplicate task_id")
        task_ids.add(value["task_id"])
        if value["status"] not in _TASK_STATES:
            raise ExecutionPacketError("task status is unsupported")
        value["dependencies"] = _strings(value["dependencies"], "task.dependencies")
        task_values.append(_freeze(value))
    if not task_values:
        raise ExecutionPacketError("tasks must not be empty")

    for collection, name in (
        (route_attempts, "route_attempts"),
        (failure_history, "failure_history"),
    ):
        if not isinstance(collection, Sequence):
            raise ExecutionPacketError(f"{name} must be a list")
        for item in collection:
            if not isinstance(item, Mapping):
                raise ExecutionPacketError(f"{name} entries must be objects")

    result = {
        "source": _freeze(source_value),
        "intent": _freeze(intent_value),
        "authority": _freeze(authority_value),
        "verification": _freeze(verification_value),
        "decisions": tuple(decision_values),
        "context_refs": tuple(context_values),
        "tasks": tuple(task_values),
        "route_attempts": tuple(_freeze(item) for item in route_attempts),
        "failure_history": tuple(_freeze(item) for item in failure_history),
    }
    _reject_forbidden(result)
    return result


__all__ = [
    "EXECUTION_PACKET_SCHEMA_VERSION",
    "SUPPORTED_SCHEMA_VERSIONS",
    "ExecutionPacket",
    "ExecutionPacketError",
    "ExecutionPacketStore",
    "PacketTransition",
    "ProofLevel",
    "UnsupportedSchemaVersionError",
    "capture_source_binding",
    "capture_worktree_digest",
    "schema_refusal_receipt",
]
