"""Durable continuity packet for one governing task.

The packet retains research, proof, and the exact next action across
interruption and root loss. It records a caller-supplied admitted route. It
does not select a route, fetch sources, construct a capture outbox, or add a
timer or restart budget.
"""

from __future__ import annotations

import fcntl
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from verdict.shared_memory import redact_secrets

_FIELDS = (
    "governing_task_id",
    "packet_revision",
    "research",
    "changed_files",
    "last_proof",
    "exact_next_action",
    "governing_revisions",
    "writer_fence",
    "replacement_writer_id",
    "admitted_route_id",
    "authorization_ref",
    "refreshed_source_ids",
    "retained_source_ids",
    "event_ids_applied",
    "capture_refs",
    "attempt_lineage_id",
)


class ContinuityPacketError(ValueError):
    """Raised when a packet write would break continuity."""


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContinuityPacketError(f"{field_name} must be a non-empty string")
    return value.strip()


def _optional_text(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _text(value, field_name)


def _task_id(value: object) -> str:
    text = _text(value, "governing_task_id")
    if "/" in text or "\\" in text or "\x00" in text or text in {".", ".."}:
        raise ContinuityPacketError("governing_task_id must be a single path segment")
    return text


def _revision(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ContinuityPacketError(f"{field_name} must be an integer >= 1")
    return value


def _exit_code(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContinuityPacketError(f"{field_name} must be an integer")
    return value


def _strings(value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ContinuityPacketError(f"{field_name} must be a list")
    return tuple(_text(item, field_name) for item in value)


def _retain(value: str, field_name: str) -> str:
    return redact_secrets(_text(value, field_name))


def _object(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ContinuityPacketError(f"{field_name} must be an object")
    return value


@dataclass(frozen=True)
class SourceRef:
    """One source revision cited by a research finding."""

    source_id: str
    revision: str

    def to_dict(self) -> dict[str, str]:
        return {"source_id": self.source_id, "revision": self.revision}

    @staticmethod
    def from_dict(value: object) -> SourceRef:
        payload = _object(value, "source_ref")
        return SourceRef(
            source_id=_text(payload.get("source_id"), "source_ref.source_id"),
            revision=_text(payload.get("revision"), "source_ref.revision"),
        )


@dataclass(frozen=True)
class ResearchFinding:
    """Incremental research kept by finding id."""

    finding_id: str
    source_refs: tuple[SourceRef, ...]
    text: str
    revision: str

    def to_dict(self) -> dict[str, object]:
        return {
            "finding_id": self.finding_id,
            "source_refs": [ref.to_dict() for ref in self.source_refs],
            "text": self.text,
            "revision": self.revision,
        }

    @staticmethod
    def from_dict(value: object) -> ResearchFinding:
        payload = _object(value, "research")
        refs = payload.get("source_refs")
        if not isinstance(refs, list) or not refs:
            raise ContinuityPacketError("research.source_refs must be a non-empty list")
        return ResearchFinding(
            finding_id=_text(payload.get("finding_id"), "research.finding_id"),
            source_refs=tuple(SourceRef.from_dict(item) for item in refs),
            text=_retain(str(payload.get("text", "")), "research.text"),
            revision=_text(payload.get("revision"), "research.revision"),
        )

    @staticmethod
    def retain(finding: ResearchFinding) -> ResearchFinding:
        if not finding.source_refs:
            raise ContinuityPacketError("research.source_refs must be a non-empty list")
        return ResearchFinding(
            finding_id=_text(finding.finding_id, "research.finding_id"),
            source_refs=tuple(
                SourceRef(
                    _text(ref.source_id, "source_ref.source_id"),
                    _text(ref.revision, "source_ref.revision"),
                )
                for ref in finding.source_refs
            ),
            text=_retain(finding.text, "research.text"),
            revision=_text(finding.revision, "research.revision"),
        )


@dataclass(frozen=True)
class LastProof:
    """A command, exit, and artifact reference, or an explicit none."""

    kind: str
    command_text: str | None = None
    exit_code: int | None = None
    artifact_ref: str | None = None

    @staticmethod
    def none() -> LastProof:
        return LastProof(kind="none")

    @staticmethod
    def command(*, command: str, exit_code: int, artifact_ref: str) -> LastProof:
        return LastProof(
            kind="command",
            command_text=_retain(command, "last_proof.command"),
            exit_code=_exit_code(exit_code, "last_proof.exit_code"),
            artifact_ref=_retain(artifact_ref, "last_proof.artifact_ref"),
        )

    def to_dict(self) -> dict[str, object]:
        if self.kind == "none":
            return {"kind": "none"}
        return {
            "kind": "command",
            "command": self.command_text,
            "exit_code": self.exit_code,
            "artifact_ref": self.artifact_ref,
        }

    @staticmethod
    def from_dict(value: object) -> LastProof:
        payload = _object(value, "last_proof")
        kind = _text(payload.get("kind"), "last_proof.kind")
        if kind == "none":
            return LastProof.none()
        if kind != "command":
            raise ContinuityPacketError("last_proof.kind must be 'command' or 'none'")
        command = payload.get("command")
        artifact = payload.get("artifact_ref")
        if not isinstance(command, str) or not isinstance(artifact, str):
            raise ContinuityPacketError("last_proof command requires command and artifact_ref")
        return LastProof.command(
            command=command,
            exit_code=_exit_code(payload.get("exit_code"), "last_proof.exit_code"),
            artifact_ref=artifact,
        )


@dataclass(frozen=True)
class WriterFence:
    """Previous writer id and the time the fence was recorded."""

    writer_id: str
    fenced_at: str

    def to_dict(self) -> dict[str, str]:
        return {"writer_id": self.writer_id, "fenced_at": self.fenced_at}

    @staticmethod
    def from_dict(value: object) -> WriterFence:
        payload = _object(value, "writer_fence")
        return WriterFence(
            writer_id=_text(payload.get("writer_id"), "writer_fence.writer_id"),
            fenced_at=_text(payload.get("fenced_at"), "writer_fence.fenced_at"),
        )


@dataclass(frozen=True)
class GoverningRevisions:
    """Acceptance, spec, and ADR revisions stored for the gate. Not compared here."""

    acceptance_criterion_ids: tuple[str, ...]
    spec_revision_digest: str
    adr_id: str
    adr_status: str

    def to_dict(self) -> dict[str, object]:
        return {
            "acceptance_criterion_ids": list(self.acceptance_criterion_ids),
            "spec_revision_digest": self.spec_revision_digest,
            "adr_id": self.adr_id,
            "adr_status": self.adr_status,
        }

    @staticmethod
    def from_dict(value: object) -> GoverningRevisions:
        payload = _object(value, "governing_revisions")
        return GoverningRevisions(
            acceptance_criterion_ids=_strings(
                payload.get("acceptance_criterion_ids"),
                "governing_revisions.acceptance_criterion_ids",
            ),
            spec_revision_digest=_text(
                payload.get("spec_revision_digest"), "governing_revisions.spec_revision_digest"
            ),
            adr_id=_text(payload.get("adr_id"), "governing_revisions.adr_id"),
            adr_status=_text(payload.get("adr_status"), "governing_revisions.adr_status"),
        )


@dataclass(frozen=True)
class ContinuityEvent:
    """One idempotent packet change. A repeated event id does not apply again."""

    event_id: str
    finding: ResearchFinding | None = None
    changed_files: tuple[str, ...] = ()
    last_proof: LastProof | None = None
    exact_next_action: str | None = None
    capture_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class ContinuityPacket:
    """Frozen continuity record for one governing task."""

    governing_task_id: str
    packet_revision: int
    research: tuple[ResearchFinding, ...]
    changed_files: tuple[str, ...]
    last_proof: LastProof
    exact_next_action: str
    governing_revisions: GoverningRevisions
    writer_fence: WriterFence | None
    replacement_writer_id: str | None
    admitted_route_id: str | None
    authorization_ref: str | None
    refreshed_source_ids: tuple[str, ...]
    retained_source_ids: tuple[str, ...]
    event_ids_applied: tuple[str, ...]
    capture_refs: tuple[str, ...]
    attempt_lineage_id: str

    def to_dict(self) -> dict[str, object]:
        fence = None if self.writer_fence is None else self.writer_fence.to_dict()
        return {
            "governing_task_id": self.governing_task_id,
            "packet_revision": self.packet_revision,
            "research": [item.to_dict() for item in self.research],
            "changed_files": list(self.changed_files),
            "last_proof": self.last_proof.to_dict(),
            "exact_next_action": self.exact_next_action,
            "governing_revisions": self.governing_revisions.to_dict(),
            "writer_fence": fence,
            "replacement_writer_id": self.replacement_writer_id,
            "admitted_route_id": self.admitted_route_id,
            "authorization_ref": self.authorization_ref,
            "refreshed_source_ids": list(self.refreshed_source_ids),
            "retained_source_ids": list(self.retained_source_ids),
            "event_ids_applied": list(self.event_ids_applied),
            "capture_refs": list(self.capture_refs),
            "attempt_lineage_id": self.attempt_lineage_id,
        }

    @staticmethod
    def from_dict(value: object) -> ContinuityPacket:
        payload = _object(value, "continuity_packet")
        unknown = sorted(set(payload) - set(_FIELDS))
        missing = sorted(set(_FIELDS) - set(payload))
        if unknown:
            raise ContinuityPacketError(f"continuity_packet has unknown field(s): {unknown}")
        if missing:
            raise ContinuityPacketError(f"continuity_packet missing field(s): {missing}")
        research = payload["research"]
        if not isinstance(research, list):
            raise ContinuityPacketError("research must be a list")
        fence = payload["writer_fence"]
        return ContinuityPacket(
            governing_task_id=_task_id(payload["governing_task_id"]),
            packet_revision=_revision(payload["packet_revision"], "packet_revision"),
            research=tuple(ResearchFinding.from_dict(item) for item in research),
            changed_files=_strings(payload["changed_files"], "changed_files"),
            last_proof=LastProof.from_dict(payload["last_proof"]),
            exact_next_action=_retain(str(payload["exact_next_action"]), "exact_next_action"),
            governing_revisions=GoverningRevisions.from_dict(payload["governing_revisions"]),
            writer_fence=None if fence is None else WriterFence.from_dict(fence),
            replacement_writer_id=_optional_text(
                payload["replacement_writer_id"], "replacement_writer_id"
            ),
            admitted_route_id=_optional_text(payload["admitted_route_id"], "admitted_route_id"),
            authorization_ref=_optional_text(payload["authorization_ref"], "authorization_ref"),
            refreshed_source_ids=_strings(payload["refreshed_source_ids"], "refreshed_source_ids"),
            retained_source_ids=_strings(payload["retained_source_ids"], "retained_source_ids"),
            event_ids_applied=_strings(payload["event_ids_applied"], "event_ids_applied"),
            capture_refs=tuple(
                _retain(item, "capture_refs")
                for item in _strings(payload["capture_refs"], "capture_refs")
            ),
            attempt_lineage_id=_text(payload["attempt_lineage_id"], "attempt_lineage_id"),
        )


def _append_unique(existing: tuple[str, ...], extra: tuple[str, ...]) -> tuple[str, ...]:
    values = list(existing)
    for item in extra:
        if item not in values:
            values.append(item)
    return tuple(values)


def _source_sets(
    packet: ContinuityPacket, source_revisions: Mapping[str, str]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    known: dict[str, set[str]] = {}
    for finding in packet.research:
        for ref in finding.source_refs:
            known.setdefault(ref.source_id, set()).add(ref.revision)
    retained: list[str] = []
    refreshed: list[str] = []
    for source_id in sorted(known):
        current = source_revisions.get(source_id)
        if current is not None and known[source_id] == {current}:
            retained.append(source_id)
        else:
            refreshed.append(source_id)
    for source_id in sorted(source_revisions):
        if source_id not in known:
            refreshed.append(source_id)
    return tuple(retained), tuple(refreshed)


def _reject_fenced_writer(packet: ContinuityPacket, writer_id: str | None) -> None:
    fence = packet.writer_fence
    if fence is None:
        return
    pending_cutover = (
        packet.replacement_writer_id is None or fence.writer_id == packet.replacement_writer_id
    )
    if pending_cutover or writer_id == fence.writer_id or writer_id != packet.replacement_writer_id:
        raise ContinuityPacketError("fenced writer cannot apply events before cutover completes")


class ContinuityPacketStore:
    """Atomic JSON record for one governing task. A new process reads the same file."""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory).expanduser().resolve()

    def _path(self, governing_task_id: str) -> Path:
        return self.directory / f"{_task_id(governing_task_id)}.json"

    def _read_file(self, path: Path) -> ContinuityPacket:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ContinuityPacketError(f"continuity packet not found: {path.name}") from exc
        except (OSError, ValueError) as exc:
            raise ContinuityPacketError(
                f"continuity packet is not valid JSON: {path.name}"
            ) from exc
        return ContinuityPacket.from_dict(payload)

    def _write(self, path: Path, packet: ContinuityPacket) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(
            json.dumps(packet.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)

    def _mutate(
        self, governing_task_id: str, change: Callable[[ContinuityPacket | None], ContinuityPacket]
    ) -> ContinuityPacket:
        path = self._path(governing_task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.with_suffix(".lock")
        with lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            current = self._read_file(path) if path.exists() else None
            updated = change(current)
            if updated.governing_task_id != _task_id(governing_task_id):
                raise ContinuityPacketError("governing task mismatch")
            self._write(path, updated)
            return updated

    def read(self, governing_task_id: str) -> ContinuityPacket:
        """Read the last successful write. Does not fetch sources or select a route."""
        return self._read_file(self._path(governing_task_id))

    def create(
        self,
        *,
        governing_task_id: str,
        exact_next_action: str,
        last_proof: LastProof,
        governing_revisions: GoverningRevisions,
        attempt_lineage_id: str,
        admitted_route_id: str | None = None,
        authorization_ref: str | None = None,
        capture_refs: tuple[str, ...] = (),
    ) -> ContinuityPacket:
        task_id = _task_id(governing_task_id)
        packet = ContinuityPacket(
            governing_task_id=task_id,
            packet_revision=1,
            research=(),
            changed_files=(),
            last_proof=last_proof,
            exact_next_action=_retain(exact_next_action, "exact_next_action"),
            governing_revisions=governing_revisions,
            writer_fence=None,
            replacement_writer_id=None,
            admitted_route_id=_optional_text(admitted_route_id, "admitted_route_id"),
            authorization_ref=_optional_text(authorization_ref, "authorization_ref"),
            refreshed_source_ids=(),
            retained_source_ids=(),
            event_ids_applied=(),
            capture_refs=tuple(_retain(item, "capture_refs") for item in capture_refs),
            attempt_lineage_id=_text(attempt_lineage_id, "attempt_lineage_id"),
        )

        def _create(current: ContinuityPacket | None) -> ContinuityPacket:
            if current is not None:
                raise ContinuityPacketError(f"continuity packet already exists: {task_id}")
            return packet

        return self._mutate(task_id, _create)

    def apply_event(
        self, governing_task_id: str, event: ContinuityEvent, *, writer_id: str | None = None
    ) -> ContinuityPacket:
        event_id = _text(event.event_id, "event_id")

        def _apply(current: ContinuityPacket | None) -> ContinuityPacket:
            if current is None:
                raise ContinuityPacketError(f"continuity packet not found: {governing_task_id}")
            if event_id in current.event_ids_applied:
                return current
            _reject_fenced_writer(current, writer_id)
            research = list(current.research)
            if event.finding is not None:
                finding = ResearchFinding.retain(event.finding)
                if all(item.finding_id != finding.finding_id for item in research):
                    research.append(finding)
            proof = current.last_proof if event.last_proof is None else event.last_proof
            action = current.exact_next_action
            if event.exact_next_action is not None:
                action = _retain(event.exact_next_action, "exact_next_action")
            return ContinuityPacket(
                governing_task_id=current.governing_task_id,
                packet_revision=current.packet_revision + 1,
                research=tuple(research),
                changed_files=_append_unique(
                    current.changed_files,
                    tuple(_text(item, "changed_files") for item in event.changed_files),
                ),
                last_proof=proof,
                exact_next_action=action,
                governing_revisions=current.governing_revisions,
                writer_fence=current.writer_fence,
                replacement_writer_id=current.replacement_writer_id,
                admitted_route_id=current.admitted_route_id,
                authorization_ref=current.authorization_ref,
                refreshed_source_ids=current.refreshed_source_ids,
                retained_source_ids=current.retained_source_ids,
                event_ids_applied=(*current.event_ids_applied, event_id),
                capture_refs=_append_unique(
                    current.capture_refs,
                    tuple(_retain(item, "capture_refs") for item in event.capture_refs),
                ),
                attempt_lineage_id=current.attempt_lineage_id,
            )

        return self._mutate(governing_task_id, _apply)

    def fence_writer(
        self, governing_task_id: str, *, writer_id: str, fenced_at: str
    ) -> ContinuityPacket:
        """Record the previous writer and fence time. Does not record a replacement."""
        fence = WriterFence(
            writer_id=_text(writer_id, "writer_id"), fenced_at=_text(fenced_at, "fenced_at")
        )

        def _fence(current: ContinuityPacket | None) -> ContinuityPacket:
            if current is None:
                raise ContinuityPacketError(f"continuity packet not found: {governing_task_id}")
            if (
                current.replacement_writer_id is not None
                and fence.writer_id != current.replacement_writer_id
            ):
                raise ContinuityPacketError("fence must name the current writer")
            if current.writer_fence == fence:
                return current
            return ContinuityPacket(
                governing_task_id=current.governing_task_id,
                packet_revision=current.packet_revision + 1,
                research=current.research,
                changed_files=current.changed_files,
                last_proof=current.last_proof,
                exact_next_action=current.exact_next_action,
                governing_revisions=current.governing_revisions,
                writer_fence=fence,
                replacement_writer_id=current.replacement_writer_id,
                admitted_route_id=current.admitted_route_id,
                authorization_ref=current.authorization_ref,
                refreshed_source_ids=current.refreshed_source_ids,
                retained_source_ids=current.retained_source_ids,
                event_ids_applied=current.event_ids_applied,
                capture_refs=current.capture_refs,
                attempt_lineage_id=current.attempt_lineage_id,
            )

        return self._mutate(governing_task_id, _fence)

    def replace_writer(
        self, governing_task_id: str, *, replacement_writer_id: str
    ) -> ContinuityPacket:
        """Record a replacement only after the previous writer id and fence time exist."""
        replacement = _text(replacement_writer_id, "replacement_writer_id")

        def _replace(current: ContinuityPacket | None) -> ContinuityPacket:
            if current is None:
                raise ContinuityPacketError(f"continuity packet not found: {governing_task_id}")
            fence = current.writer_fence
            if fence is None or not fence.writer_id or not fence.fenced_at:
                raise ContinuityPacketError("replacement rejected: previous writer is not fenced")
            if (
                current.replacement_writer_id is not None
                and fence.writer_id != current.replacement_writer_id
            ):
                raise ContinuityPacketError("replacement rejected: current writer is not fenced")
            if replacement == fence.writer_id:
                raise ContinuityPacketError("replacement writer must differ from the fenced writer")
            if current.replacement_writer_id == replacement and fence.writer_id == replacement:
                return current
            return ContinuityPacket(
                governing_task_id=current.governing_task_id,
                packet_revision=current.packet_revision + 1,
                research=current.research,
                changed_files=current.changed_files,
                last_proof=current.last_proof,
                exact_next_action=current.exact_next_action,
                governing_revisions=current.governing_revisions,
                writer_fence=fence,
                replacement_writer_id=replacement,
                admitted_route_id=current.admitted_route_id,
                authorization_ref=current.authorization_ref,
                refreshed_source_ids=current.refreshed_source_ids,
                retained_source_ids=current.retained_source_ids,
                event_ids_applied=current.event_ids_applied,
                capture_refs=current.capture_refs,
                attempt_lineage_id=current.attempt_lineage_id,
            )

        return self._mutate(governing_task_id, _replace)

    def resume(
        self,
        governing_task_id: str,
        *,
        requested_governing_task_id: str,
        admitted_route_id: str,
        authorization_ref: str,
        source_revisions: Mapping[str, str],
    ) -> ContinuityPacket:
        """Keep the governing task and record the caller-supplied admitted route.

        Unchanged source revisions are retained and are not fetched. Changed or
        missing source ids are marked for refresh. This method does not select
        a route and does not rewrite research.
        """
        requested = _task_id(requested_governing_task_id)
        route_id = _text(admitted_route_id, "admitted_route_id")
        authorization = _text(authorization_ref, "authorization_ref")
        revisions = {
            _text(key, "source_revisions key"): _text(item, "source_revisions value")
            for key, item in source_revisions.items()
        }

        def _resume(current: ContinuityPacket | None) -> ContinuityPacket:
            if current is None:
                raise ContinuityPacketError(f"continuity packet not found: {governing_task_id}")
            if requested != current.governing_task_id:
                raise ContinuityPacketError("governing task mismatch")
            if current.writer_fence is None:
                raise ContinuityPacketError("resume rejected: previous writer is not fenced")
            retained, refreshed = _source_sets(current, revisions)
            if (
                current.admitted_route_id == route_id
                and current.authorization_ref == authorization
                and current.retained_source_ids == retained
                and current.refreshed_source_ids == refreshed
            ):
                return current
            return ContinuityPacket(
                governing_task_id=current.governing_task_id,
                packet_revision=current.packet_revision + 1,
                research=current.research,
                changed_files=current.changed_files,
                last_proof=current.last_proof,
                exact_next_action=current.exact_next_action,
                governing_revisions=current.governing_revisions,
                writer_fence=current.writer_fence,
                replacement_writer_id=current.replacement_writer_id,
                admitted_route_id=route_id,
                authorization_ref=authorization,
                refreshed_source_ids=refreshed,
                retained_source_ids=retained,
                event_ids_applied=current.event_ids_applied,
                capture_refs=current.capture_refs,
                attempt_lineage_id=current.attempt_lineage_id,
            )

        return self._mutate(governing_task_id, _resume)


def fence_writer(
    record_dir: str | Path, governing_task_id: str, *, writer_id: str, fenced_at: str
) -> ContinuityPacket:
    """Record the previous writer id and fence time before any replacement."""
    return ContinuityPacketStore(record_dir).fence_writer(
        governing_task_id, writer_id=writer_id, fenced_at=fenced_at
    )


def replace_writer(
    record_dir: str | Path, governing_task_id: str, *, replacement_writer_id: str
) -> ContinuityPacket:
    """Record a replacement writer id only when a fence is already stored."""
    return ContinuityPacketStore(record_dir).replace_writer(
        governing_task_id, replacement_writer_id=replacement_writer_id
    )
