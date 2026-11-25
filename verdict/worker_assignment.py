"""Signed worker assignments: the credential a worker presents to ``verdict serve``.

An assignment is one opaque bearer string::

    vwa1.<payload>.<tag>
    payload = base64url(JSON {"exp", "iat", "node_id", "route_id", "run_id"})    # unpadded
    tag     = base64url(HMAC-SHA256(key, "verdict-worker-assignment/v1\\n" + payload))

``route_id``, ``run_id`` and ``node_id`` are non-empty strings of at most 128 characters without
control characters.  ``exp`` (expiry) and ``iat`` (issued at) are integer seconds since the Unix
epoch, UTC, from 0 through 253402300799 (9999-12-31T23:59:59Z, the last second a UTC ``datetime``
holds), the lifetime ``exp - iat`` is from 0 through 1,800 seconds, and ``iat`` is at most 60
seconds ahead of the verifier's clock.  Base64url is the
unpadded URL-safe alphabet and is decoded strictly (no padding, no other characters, canonical
bits only).

The verifier is pure: no I/O except :func:`load_assignment_key`, no caching, nothing read from
the process's variables, standard library only.  A refusal is one :class:`AssignmentError` carrying a reason code
and nothing taken from the token, so a caller can log the code without logging the credential.
The caller (``verdict serve``) answers every refusal with the same generic body.

:func:`mint_assignment` is the production signer, used by the OpenCode executor (#1325).  It is
as pure as the verifier (key bytes and a clock in, a token out) and refuses, with ``ValueError``,
anything the verifier would refuse, so a token that is returned is one serve can accept.  The
tests sign with an independent helper (``tests/worker_assignment_helpers.py``) written from the
format above and prove that it and this signer give the same token for the same payload and key,
so each checks the other against the format.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

#: The format version, the first part of every assignment.
VERSION = "vwa1"
#: Mixed into the MAC so a tag made for this purpose is not valid for any other use of the key.
DOMAIN = b"verdict-worker-assignment/v1\n"
#: A longer token is refused before it is decoded.
MAX_TOKEN_CHARS = 2048
#: Longest route, run or node id.
MAX_ID_CHARS = 128
#: 9999-12-31T23:59:59Z: the last second a UTC ``datetime`` can hold.
MAX_EPOCH_SECONDS = 253402300799
#: ``exp - iat`` may be at most this (twice the 900 s attempt ceiling in the orchestrator).
MAX_LIFETIME_SECONDS = 1800
#: An ``iat`` further ahead of the clock than this is refused.  The lifetime cap alone bounds only
#: the declared lifetime: a token dated years ahead with a one-second lifetime would be usable from
#: now until then.  With this bound the usable window of any accepted token is at most
#: ``MAX_LIFETIME_SECONDS + MAX_CLOCK_SKEW_SECONDS`` from now.  It also lets a signer whose clock
#: runs a little ahead of the verifier's be accepted.
MAX_CLOCK_SKEW_SECONDS = 60
#: HMAC-SHA256 output size, and the size of the decoded tag.
_TAG_BYTES = 32

REASON_MALFORMED = "malformed"
REASON_BAD_SIGNATURE = "bad_signature"
REASON_EXPIRED = "expired"
REASON_KEY_UNAVAILABLE = "key_unavailable"
REASONS = frozenset(
    {REASON_MALFORMED, REASON_BAD_SIGNATURE, REASON_EXPIRED, REASON_KEY_UNAVAILABLE}
)

_FIELDS = frozenset({"route_id", "run_id", "node_id", "exp", "iat"})
_ID_FIELDS = ("route_id", "run_id", "node_id")
_B64URL = re.compile(r"[A-Za-z0-9_-]*")
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
#: The signing key is at least this many bytes, and ``generate_assignment_key`` makes exactly this
#: many.  A shorter key is never used: HMAC with a weak key would let anyone forge an assignment.
MIN_KEY_BYTES = 32
#: ``~/.config/verdict/<this>``: beside ``serve.env``, never inside it.
KEY_FILE_NAME = "worker-assignment.key"
#: A key file larger than this is not a key; it is refused without being decoded.
_MAX_KEY_FILE_BYTES = 4096


class AssignmentError(Exception):
    """An assignment or its key was refused.  The message is the reason code, never the token."""

    def __init__(self, reason: str) -> None:
        if reason not in REASONS:
            raise ValueError(f"unknown assignment refusal reason: {reason!r}")
        super().__init__(reason)
        self.reason = reason


_SEAL = object()


@dataclass(frozen=True)
class VerifiedWorkerAssignment:
    """What a verified assignment authorises: one route, for one run and node, until a time.

    Only :func:`verify_assignment` can build one (it holds a module-private seal), and the relay
    reads it from ``request.state`` and the routing context, neither of which a request body or
    a header can write.  ``assignment_digest`` is the SHA-256 (hex) of the received payload text,
    never of the token, so receipts can name the assignment without carrying a credential.
    """

    route_id: str
    run_id: str
    node_id: str
    expires_at: datetime
    assignment_digest: str
    _seal: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._seal is not _SEAL:
            raise TypeError("VerifiedWorkerAssignment is built only by verify_assignment")


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    """Strict unpadded base64url; ``ValueError`` for anything else, trailing bits included."""
    if not _B64URL.fullmatch(text) or len(text) % 4 == 1:
        raise ValueError("not unpadded base64url")
    raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    if _b64url_encode(raw) != text:
        raise ValueError("non-canonical base64url")
    return raw


def _utc_from_epoch(seconds: int) -> datetime:
    return _EPOCH + timedelta(seconds=seconds)


def _epoch_seconds(moment: datetime) -> int:
    """Whole seconds since the epoch (rounded down) without going through a float."""
    return (moment - _EPOCH) // timedelta(seconds=1)


def _is_second(value: Any) -> bool:
    return type(value) is int and 0 <= value <= MAX_EPOCH_SECONDS


def _is_id(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= MAX_ID_CHARS
        and not any(unicodedata.category(char) in {"Cc", "Cs"} for char in value)
    )


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate key")
    return dict(pairs)


def _payload_fields(payload_text: str) -> dict[str, Any]:
    """Decode and validate the payload; ``AssignmentError(malformed)`` for any defect."""
    try:
        decoded = json.loads(
            _b64url_decode(payload_text).decode("utf-8"), object_pairs_hook=_no_duplicate_keys
        )
    except (ValueError, RecursionError) as exc:  # not base64url, not UTF-8, not JSON, too deep
        raise AssignmentError(REASON_MALFORMED) from exc
    if not isinstance(decoded, dict) or set(decoded) != _FIELDS:
        raise AssignmentError(REASON_MALFORMED)
    if not all(_is_id(decoded[name]) for name in _ID_FIELDS):
        raise AssignmentError(REASON_MALFORMED)
    exp, iat = decoded["exp"], decoded["iat"]
    # The ranges come before the lifetime arithmetic and the timestamp conversion, so no value
    # that is not a representable UTC second goes any further.
    if not (_is_second(exp) and _is_second(iat)):
        raise AssignmentError(REASON_MALFORMED)
    if not 0 <= exp - iat <= MAX_LIFETIME_SECONDS:
        raise AssignmentError(REASON_MALFORMED)
    return decoded


def verify_assignment(token: Any, key: bytes, *, now: datetime) -> VerifiedWorkerAssignment:
    """Verify ``token`` with ``key`` at ``now`` or raise :class:`AssignmentError`.

    Order: key size, token size and shape (before any decoding of the payload), the tag with
    ``hmac.compare_digest`` over the received payload text, and only then the payload (shape,
    field limits, the ranges of ``exp`` and ``iat``, the lifetime), ``iat`` against the clock, the
    conversion of ``exp`` to a UTC time and the expiry (strictly after ``now``).  ``now`` must carry a UTC offset.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be a datetime with a timezone offset")
    if not isinstance(key, (bytes, bytearray)) or len(key) < MIN_KEY_BYTES:
        raise AssignmentError(REASON_KEY_UNAVAILABLE)
    if not isinstance(token, str) or len(token) > MAX_TOKEN_CHARS or not token.isascii():
        raise AssignmentError(REASON_MALFORMED)
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != VERSION:
        raise AssignmentError(REASON_MALFORMED)
    _, payload_text, tag_text = parts
    try:
        received_tag = _b64url_decode(tag_text)
    except ValueError as exc:
        raise AssignmentError(REASON_MALFORMED) from exc
    if len(received_tag) != _TAG_BYTES:
        raise AssignmentError(REASON_MALFORMED)
    expected_tag = hmac.new(bytes(key), DOMAIN + payload_text.encode("ascii"), hashlib.sha256)
    if not hmac.compare_digest(received_tag, expected_tag.digest()):
        raise AssignmentError(REASON_BAD_SIGNATURE)
    fields = _payload_fields(payload_text)
    if fields["iat"] > now.timestamp() + MAX_CLOCK_SKEW_SECONDS:
        raise AssignmentError(REASON_MALFORMED)  # dated ahead: its usable window is not bounded
    try:
        expires_at = _utc_from_epoch(fields["exp"])
    except (OverflowError, ValueError, OSError) as exc:
        raise AssignmentError(REASON_MALFORMED) from exc
    if not expires_at > now:
        raise AssignmentError(REASON_EXPIRED)
    return VerifiedWorkerAssignment(
        route_id=fields["route_id"],
        run_id=fields["run_id"],
        node_id=fields["node_id"],
        expires_at=expires_at,
        assignment_digest=hashlib.sha256(payload_text.encode("ascii")).hexdigest(),
        _seal=_SEAL,
    )


def mint_assignment(
    key: bytes, *, route_id: str, run_id: str, node_id: str, lifetime_seconds: int, now: datetime
) -> str:
    """Sign an assignment for one route, run and node, valid from ``now`` for ``lifetime_seconds``.

    ``iat`` is ``now`` in whole seconds (rounded down) and ``exp`` is ``iat + lifetime_seconds``,
    so the lifetime the verifier checks is exactly the one asked for.  Nothing is clamped: a
    lifetime that is not a whole number of seconds from 1 through :data:`MAX_LIFETIME_SECONDS`,
    an id the verifier would refuse (empty, over :data:`MAX_ID_CHARS`, or holding a control
    character), a key shorter than :data:`MIN_KEY_BYTES`, a clock without a UTC offset, an ``exp``
    or ``iat`` that is not a representable UTC second, or a token longer than
    :data:`MAX_TOKEN_CHARS` is a ``ValueError``, and no token is returned.  The message names the
    rule, never the key or an id.

    The payload is the JSON object with its keys sorted, written compactly, with non-ASCII
    escaped (the form the format's reference signer produces), then base64url without padding.
    """
    if not isinstance(key, (bytes, bytearray)) or len(key) < MIN_KEY_BYTES:
        raise ValueError(f"the signing key must be at least {MIN_KEY_BYTES} bytes")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be a datetime with a timezone offset")
    if type(lifetime_seconds) is not int or not 1 <= lifetime_seconds <= MAX_LIFETIME_SECONDS:
        raise ValueError(
            f"the lifetime must be a whole number of seconds from 1 through {MAX_LIFETIME_SECONDS}"
        )
    ids = {"route_id": route_id, "run_id": run_id, "node_id": node_id}
    for name, value in ids.items():
        if not _is_id(value):
            raise ValueError(
                f"{name} must be a string of 1 to {MAX_ID_CHARS} characters without control "
                "characters"
            )
    try:
        issued = _epoch_seconds(now)
    except OverflowError as exc:
        raise ValueError("the clock is not a representable UTC second") from exc
    expires = issued + lifetime_seconds
    if not (_is_second(issued) and _is_second(expires)):
        raise ValueError("the clock is not a representable UTC second")
    payload = {**ids, "exp": expires, "iat": issued}
    payload_text = _b64url_encode(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    tag = hmac.new(bytes(key), DOMAIN + payload_text.encode("ascii"), hashlib.sha256).digest()
    token = f"{VERSION}.{payload_text}.{_b64url_encode(tag)}"
    if len(token) > MAX_TOKEN_CHARS:
        raise ValueError(f"the assignment would be longer than {MAX_TOKEN_CHARS} characters")
    return token


def assignment_digest(token: str) -> str:
    """The SHA-256 (hex) of the payload text of ``token``: what a verifier records for it.

    The same value :func:`verify_assignment` puts in ``assignment_digest``, so a signer can name
    its own assignment in a receipt serve wrote without the credential itself.  It reads the
    token's shape only and does not check the tag; ``ValueError`` for anything that is not three
    ``.``-separated parts of ASCII.
    """
    parts = token.split(".") if isinstance(token, str) and token.isascii() else []
    if len(parts) != 3 or parts[0] != VERSION or not parts[1]:
        raise ValueError("not a worker assignment")
    return hashlib.sha256(parts[1].encode("ascii")).hexdigest()


def assignment_key_path(home: Path | None = None) -> Path:
    """``~/.config/verdict/worker-assignment.key``, beside ``serve.env`` (the unit's ``%h`` form)."""
    return (home or Path.home()) / ".config" / "verdict" / KEY_FILE_NAME


def load_assignment_key(home: Path | None = None) -> bytes:
    """Read and decode the signing key; ``AssignmentError(key_unavailable)`` for any defect.

    Reads the file on every call and keeps nothing, so a rotation (remove the file, run setup
    again) takes effect on the next request.  The key is stored as unpadded URL-safe base64 text
    and must decode to at least :data:`MIN_KEY_BYTES` bytes.  The path and the key never appear
    in the error.
    """
    try:
        with assignment_key_path(home).open("rb") as handle:
            data = handle.read(_MAX_KEY_FILE_BYTES + 1)
        if len(data) > _MAX_KEY_FILE_BYTES:
            raise ValueError("key file too large")
        key = _b64url_decode(data.decode("ascii").strip())
    except (OSError, RuntimeError, ValueError) as exc:  # unreadable, no home, not a base64url key
        raise AssignmentError(REASON_KEY_UNAVAILABLE) from exc
    if len(key) < MIN_KEY_BYTES:
        raise AssignmentError(REASON_KEY_UNAVAILABLE)
    return key


def generate_assignment_key() -> str:
    """A new signing key: 32 random bytes as unpadded URL-safe base64 text."""
    return _b64url_encode(secrets.token_bytes(MIN_KEY_BYTES))
