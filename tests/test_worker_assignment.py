"""Format and verification of the signed worker assignment (serve-worker-assignment, group 1).

Tokens are signed by ``tests/worker_assignment_helpers.py``, which is written from the format in
the spec and shares no code with the verifier, so these tests check the verifier against the
written format rather than against itself.  Nothing here touches the real home or the network.
"""

from __future__ import annotations

import dataclasses
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from tests import worker_assignment_helpers as sign
from verdict import worker_assignment as wa

KEY = bytes(range(32))
OTHER_KEY = bytes(range(1, 33))
NOW = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
NOW_S = int(NOW.timestamp())
FUTURE = NOW_S + 900


def _verify(token: Any, key: bytes = KEY, now: datetime = NOW) -> wa.VerifiedWorkerAssignment:
    return wa.verify_assignment(token, key, now=now)


def _refusal(token: Any, key: bytes = KEY, now: datetime = NOW) -> str:
    """The reason code of the refusal; fails the test when the token is accepted."""
    with pytest.raises(wa.AssignmentError) as raised:
        wa.verify_assignment(token, key, now=now)
    return raised.value.reason


def test_a_valid_assignment_yields_route_run_node_and_expiry() -> None:
    token = sign.assignment(KEY, route_id="omniroute/a", run_id="r9", node_id="n3", exp=FUTURE)
    verified = _verify(token)
    assert verified.route_id == "omniroute/a"
    assert verified.run_id == "r9"
    assert verified.node_id == "n3"
    assert verified.expires_at == datetime.fromtimestamp(FUTURE, tz=timezone.utc)
    assert verified.expires_at.utcoffset() == timedelta(0)
    # The digest is of the payload text the tag covers, never of the token.
    assert (
        verified.assignment_digest == hashlib.sha256(sign.payload_part(token).encode()).hexdigest()
    )
    assert verified.assignment_digest not in token
    assert token not in repr(verified)


def test_the_verified_type_is_frozen_and_cannot_be_built_by_hand() -> None:
    verified = _verify(sign.assignment(KEY, exp=FUTURE))
    with pytest.raises(dataclasses.FrozenInstanceError):
        verified.route_id = "other"  # type: ignore[misc]
    with pytest.raises(TypeError):
        wa.VerifiedWorkerAssignment(  # type: ignore[call-arg]
            route_id="r", run_id="r", node_id="n", expires_at=NOW, assignment_digest="d"
        )


def test_a_tag_made_with_another_key_is_a_bad_signature() -> None:
    assert _refusal(sign.assignment(OTHER_KEY, exp=FUTURE)) == "bad_signature"


def test_a_wrong_verification_key_is_a_bad_signature() -> None:
    assert _refusal(sign.assignment(KEY, exp=FUTURE), key=OTHER_KEY) == "bad_signature"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("route_id", "omniroute/other"),
        ("run_id", "run-2"),
        ("node_id", "node-2"),
        ("exp", FUTURE + 1),
        ("iat", NOW_S - 1),
    ],
)
def test_each_signed_field_is_covered_by_the_tag(field: str, value: Any) -> None:
    token = sign.assignment(KEY, exp=FUTURE, iat=NOW_S)
    assert _verify(token)  # the control: the untouched token verifies
    tampered = sign.retag_with_changed(token, **{field: value})
    assert _refusal(tampered) == "bad_signature"


_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


def test_a_tag_with_non_canonical_trailing_bits_is_malformed() -> None:
    # The last of the 43 tag characters carries two bits that must be zero.  Flipping one decodes
    # to the same 32 bytes, so a lenient decoder would accept a second spelling of one tag.
    token = sign.assignment(KEY, exp=FUTURE)
    tag = sign.tag_part(token)
    assert _verify(token)
    variant = tag[:-1] + _ALPHABET[_ALPHABET.index(tag[-1]) ^ 1]
    assert variant != tag
    assert _refusal(f"vwa1.{sign.payload_part(token)}.{variant}") == "malformed"


def test_a_tag_of_the_right_shape_that_is_not_the_mac_is_a_bad_signature() -> None:
    token = sign.assignment(KEY, exp=FUTURE)
    forged = f"vwa1.{sign.payload_part(token)}.{sign.b64url(bytes(32))}"
    assert _refusal(forged) == "bad_signature"


def test_expiry_must_be_strictly_after_the_clock() -> None:
    assert _verify(sign.assignment(KEY, exp=NOW_S + 1)).expires_at == NOW + timedelta(seconds=1)
    assert _refusal(sign.assignment(KEY, exp=NOW_S)) == "expired"
    assert _refusal(sign.assignment(KEY, exp=NOW_S - 1)) == "expired"


def test_a_naive_clock_is_a_programming_error_not_a_refusal() -> None:
    with pytest.raises(ValueError, match="timezone"):
        _verify(sign.assignment(KEY, exp=FUTURE), now=datetime(2026, 11, 24, 12, 0))


def _good() -> str:
    return sign.assignment(KEY, exp=FUTURE)


def _shape_cases() -> list[Any]:
    good = _good()
    payload, tag = sign.payload_part(good), sign.tag_part(good)
    return [
        pytest.param("", id="empty"),
        pytest.param("vwa1", id="one-part"),
        pytest.param(f"vwa1.{payload}", id="two-parts"),
        pytest.param(f"vwa1.{payload}.{tag}.x", id="four-parts"),
        pytest.param(f"vwa2.{payload}.{tag}", id="wrong-version"),
        pytest.param(f"VWA1.{payload}.{tag}", id="wrong-case-version"),
        pytest.param(f"vwa1.{payload}.{tag[:-1]}", id="short-tag"),
        pytest.param(f"vwa1.{payload}.{tag}A", id="long-tag"),
        pytest.param(f"vwa1.{payload}.", id="empty-tag"),
        pytest.param(f"vwa1.{payload}.{tag[:-1]}!", id="tag-not-base64url"),
        pytest.param(f"vwa1.{payload}.{tag[:-1]}é", id="tag-not-ascii"),
        pytest.param(f"vwa1.{payload}.{tag}=", id="tag-padded"),
        pytest.param(f" vwa1.{payload}.{tag}", id="leading-space"),
        pytest.param(f"vwa1.{payload}.{tag}\n", id="trailing-newline"),
        pytest.param(None, id="none"),
        pytest.param(b"vwa1.x.y", id="bytes"),
        pytest.param(12, id="int"),
    ]


@pytest.mark.parametrize("token", _shape_cases())
def test_a_token_that_is_not_three_well_formed_parts_is_malformed(token: Any) -> None:
    assert _refusal(token) == "malformed"


def _signed_payload_cases() -> list[Any]:
    """Payloads signed with the right key, so only the payload rules can refuse them."""
    full = sign.payload_fields(exp=FUTURE, iat=NOW_S)
    cases: list[Any] = [
        pytest.param(sign.sign_payload_text("not*base64url", KEY), id="payload-not-base64url"),
        pytest.param(sign.sign_payload_text("e30=", KEY), id="payload-padded"),
        pytest.param(sign.sign_payload_text("a", KEY), id="payload-impossible-length"),
        pytest.param(sign.sign_payload_text(sign.b64url(b"not json"), KEY), id="payload-not-json"),
        pytest.param(sign.sign_payload_text(sign.b64url(b"\xff\xfe"), KEY), id="payload-not-utf8"),
        pytest.param(sign.sign_payload_text("", KEY), id="payload-empty"),
        pytest.param(sign.sign_object([1, 2], KEY), id="payload-list"),
        pytest.param(sign.sign_object("text", KEY), id="payload-string"),
        pytest.param(sign.sign_object(None, KEY), id="payload-null"),
        pytest.param(sign.sign_object({}, KEY), id="payload-empty-object"),
        pytest.param(
            # Unclosed, so the parser recurses until it gives up: the token stays under 2 KiB.
            sign.sign_payload_text(sign.b64url(b"[" * 1400), KEY),
            id="payload-deeply-nested",
        ),
        pytest.param(
            sign.sign_raw_json(
                '{"exp":2000000000,"exp":2000000001,"iat":1999999100,'
                '"node_id":"n","route_id":"r","run_id":"u"}',
                KEY,
            ),
            id="duplicate-key",
        ),
        pytest.param(sign.sign_object({**full, "extra": 1}, KEY), id="extra-field"),
    ]
    for name in full:
        cases.append(
            pytest.param(
                sign.sign_object({k: v for k, v in full.items() if k != name}, KEY),
                id=f"missing-{name}",
            )
        )
    for name in ("route_id", "run_id", "node_id"):
        for label, bad in (
            ("empty", ""),
            ("none", None),
            ("int", 5),
            ("list", ["a"]),
            ("bool", True),
            ("too-long", "x" * 129),
            ("newline", "a\nb"),
            ("nul", "a\x00b"),
            ("del", "a\x7fb"),
            ("c1-control", "a\u0085b"),
            ("escape", "a\x1b[31mb"),
            ("lone-surrogate", "a\ud800b"),
        ):
            cases.append(
                pytest.param(
                    sign.assignment(KEY, exp=FUTURE, iat=NOW_S, **{name: bad}), id=f"{name}-{label}"
                )
            )
    for name in ("exp", "iat"):
        for label, bad in (
            ("string", "2000000000"),
            ("float", 2000000000.5),
            ("bool-true", True),
            ("bool-false", False),
            ("negative", -1),
            ("none", None),
            ("list", [1]),
        ):
            cases.append(
                pytest.param(
                    sign.assignment(KEY, **{**{"exp": FUTURE, "iat": NOW_S}, name: bad}),
                    id=f"{name}-{label}",
                )
            )
    # JSON booleans are not integers.  As Python ints they would give a valid 1 s lifetime and an
    # exp in the past, so only the type rule (not the lifetime rule) refuses them as malformed.
    cases.append(pytest.param(sign.assignment(KEY, exp=True, iat=False), id="exp-and-iat-booleans"))
    cases.append(pytest.param(sign.assignment(KEY, exp=True, iat=True), id="exp-and-iat-true"))
    # A float spelled as an integer-valued JSON number is still a float.
    cases.append(
        pytest.param(
            sign.sign_raw_json(
                '{"exp":2000000000.0,"iat":1999999100,"node_id":"n","route_id":"r","run_id":"u"}',
                KEY,
            ),
            id="exp-float-integer-valued",
        )
    )
    cases.append(
        pytest.param(
            sign.sign_raw_json(
                '{"exp":2e9,"iat":1999999100,"node_id":"n","route_id":"r","run_id":"u"}', KEY
            ),
            id="exp-exponent",
        )
    )
    return cases


@pytest.mark.parametrize("token", _signed_payload_cases())
def test_a_correctly_signed_payload_that_breaks_the_format_is_malformed(token: str) -> None:
    assert _refusal(token) == "malformed"


@pytest.mark.parametrize("exp", [253402300800, 2**63, 2**64, 10**30])
def test_an_exp_that_cannot_be_a_timestamp_is_malformed_never_an_exception(exp: int) -> None:
    # The tag is correct and the lifetime (10 s) is short, so only the bounds on exp and iat can
    # refuse it; the verifier must answer with the one refusal, never raise OverflowError,
    # ValueError or OSError out of the timestamp conversion.
    token = sign.sign_raw_json(
        f'{{"exp":{exp},"iat":{exp - 10},"node_id":"n","route_id":"r","run_id":"u"}}', KEY
    )
    assert _refusal(token) == "malformed"


def test_an_exp_one_past_the_bound_is_refused_by_its_own_bound() -> None:
    # iat is in range and the lifetime is 800 s: nothing but the bound on exp refuses this.
    token = sign.assignment(KEY, exp=253402300800, iat=253402300000)
    assert _refusal(token) == "malformed"


def test_a_parser_recursion_failure_is_malformed(monkeypatch: pytest.MonkeyPatch) -> None:
    # Whether json raises RecursionError for a payload inside the 2 KiB cap depends on the
    # interpreter (it does on some, not on 3.14), so the refusal is pinned by injection.
    def too_deep(*_args: Any, **_kwargs: Any) -> Any:
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(wa.json, "loads", too_deep)
    assert _refusal(sign.assignment(KEY, exp=FUTURE)) == "malformed"


def test_the_bound_on_exp_holds_without_the_conversion_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The bound and the conversion guard are two defences.  With a conversion that never raises,
    # only the bound can refuse an exp past the last UTC second, so this fails if it is removed.
    monkeypatch.setattr(wa, "_utc_from_epoch", lambda _seconds: NOW + timedelta(hours=1))
    token = sign.assignment(KEY, exp=253402300800, iat=253402300000)
    assert _refusal(token) == "malformed"


def test_the_largest_representable_exp_verifies_and_is_the_last_utc_second() -> None:
    token = sign.assignment(KEY, exp=sign.MAX_EXP, iat=sign.MAX_EXP - 900)
    verified = _verify(token, now=NOW)
    assert verified.expires_at == datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
    assert verified.expires_at.utcoffset() == timedelta(0)


def test_an_exp_of_zero_is_well_formed_and_expired_not_malformed() -> None:
    assert _refusal(sign.assignment(KEY, exp=0, iat=0)) == "expired"


def test_a_negative_issued_at_is_malformed_even_when_the_lifetime_is_short() -> None:
    assert _refusal(sign.assignment(KEY, exp=100, iat=-1)) == "malformed"


@pytest.mark.parametrize(
    ("lifetime", "accepted"),
    [(0, True), (1, True), (900, True), (1800, True), (1801, False), (7200, False), (-1, False)],
)
def test_the_lifetime_exp_minus_iat_is_capped_at_1800_seconds(
    lifetime: int, accepted: bool
) -> None:
    token = sign.assignment(KEY, exp=FUTURE, iat=FUTURE - lifetime)
    if accepted:
        assert _verify(token).expires_at == datetime.fromtimestamp(FUTURE, tz=timezone.utc)
    else:
        # A signed, unexpired, too-long (or negative) lifetime is refused as malformed, so a
        # mis-signed long-lived assignment cannot exist.
        assert _refusal(token) == "malformed"


def test_a_long_lifetime_is_refused_before_expiry_is_considered() -> None:
    # exp is in the past and the lifetime is too long: the format refusal wins over "expired".
    assert _refusal(sign.assignment(KEY, exp=NOW_S - 10, iat=NOW_S - 10 - 3600)) == "malformed"


def test_the_issued_at_is_not_required_to_be_in_the_past() -> None:
    # Nothing in the spec checks iat against the clock (clock skew between signer and serve);
    # only the lifetime rule applies.
    assert _verify(sign.assignment(KEY, exp=FUTURE + 600, iat=FUTURE)).route_id


@pytest.mark.parametrize("failure", [OverflowError, ValueError, OSError])
def test_a_conversion_failure_is_malformed_whatever_exception_it_raises(
    monkeypatch: pytest.MonkeyPatch, failure: type[Exception]
) -> None:
    def explode(_seconds: int) -> datetime:
        raise failure("boom")

    monkeypatch.setattr(wa, "_utc_from_epoch", explode)
    # An exp inside the documented range still reaches the conversion; the guard is what maps
    # whatever the platform raises to the one refusal.
    assert _refusal(sign.assignment(KEY, exp=FUTURE)) == "malformed"


def test_an_oversized_token_is_refused_before_it_is_decoded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decoded: list[str] = []
    real = wa._b64url_decode

    def spy(text: str) -> bytes:
        decoded.append(text)
        return real(text)

    monkeypatch.setattr(wa, "_b64url_decode", spy)
    huge = "vwa1." + "A" * 2048 + "." + "B" * 43
    assert len(huge) > 2048
    assert _refusal(huge) == "malformed"
    assert decoded == []
    # A token of exactly the cap still reaches the decoder.
    exact = "vwa1." + "A" * (2048 - len("vwa1..") - 43) + "." + "B" * 43
    assert len(exact) == 2048
    assert _refusal(exact) in {"malformed", "bad_signature"}
    assert decoded  # the cap is inclusive: 2 KiB is let through to be judged on its tag


def test_the_tag_is_compared_in_constant_time_before_the_payload_is_decoded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, Any]] = []
    real_compare = wa.hmac.compare_digest
    real_decode = wa._b64url_decode

    def compare(a: Any, b: Any) -> bool:
        events.append(("compare", None))
        return real_compare(a, b)

    def decode(text: str) -> bytes:
        events.append(("decode", text))
        return real_decode(text)

    monkeypatch.setattr(wa.hmac, "compare_digest", compare)
    monkeypatch.setattr(wa, "_b64url_decode", decode)
    token = sign.assignment(KEY, exp=FUTURE)
    payload, tag = sign.payload_part(token), sign.tag_part(token)

    # Accepted: the one compare happens before the payload is decoded.
    _verify(token)
    kinds = [kind for kind, _ in events]
    assert kinds.count("compare") == 1
    compare_at = kinds.index("compare")
    payload_decoded_at = events.index(("decode", payload))
    assert compare_at < payload_decoded_at
    # Only the tag may be decoded ahead of the compare.
    assert all(text == tag for kind, text in events[:compare_at] if kind == "decode")

    # Refused on the tag: the payload is never decoded.
    events.clear()
    assert _refusal(sign.retag_with_changed(token, run_id="other")) == "bad_signature"
    assert [kind for kind, _ in events].count("compare") == 1
    assert not any(kind == "decode" and text != tag for kind, text in events)


def test_a_refusal_names_its_reason_and_never_the_token() -> None:
    token = sign.assignment(OTHER_KEY, exp=FUTURE)
    with pytest.raises(wa.AssignmentError) as raised:
        _verify(token)
    message = str(raised.value)
    assert raised.value.reason == "bad_signature"
    assert message == "bad_signature"
    assert sign.payload_part(token) not in message
    assert sign.tag_part(token) not in message


def test_a_key_shorter_than_32_bytes_is_never_used_to_verify() -> None:
    short = b"short"
    forged = sign.assignment(short, exp=FUTURE)  # a correct tag under the weak key
    assert _refusal(forged, key=short) == "key_unavailable"
    assert _refusal(forged, key=b"") == "key_unavailable"


def test_reason_codes_are_the_documented_set() -> None:
    expected = {"malformed", "bad_signature", "expired", "key_unavailable"}
    assert set(wa.REASONS) == expected
    with pytest.raises(ValueError):
        wa.AssignmentError("invented")


# --- the key file (task 1.3) -------------------------------------------------------------


def _write_key(home: Any, text: str | bytes) -> Any:
    path = wa.assignment_key_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8")
    return path


def test_the_key_path_is_beside_serve_env_under_dot_config(tmp_path: Any) -> None:
    from verdict.serve_service import env_file_path

    assert (
        wa.assignment_key_path(tmp_path)
        == tmp_path / ".config" / "verdict" / "worker-assignment.key"
    )
    # Setup, doctor and the middleware must all name one file: it sits with serve.env.
    assert wa.assignment_key_path(tmp_path).parent == env_file_path(tmp_path).parent


def test_the_default_key_path_follows_the_home_directory(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert wa.assignment_key_path() == tmp_path / ".config" / "verdict" / "worker-assignment.key"


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(None, id="missing-file"),
        pytest.param("", id="empty-file"),
        pytest.param("   \n", id="blank-file"),
        pytest.param("not base64url!!\n", id="not-base64url"),
        pytest.param(sign.b64url(bytes(31)), id="31-bytes"),
        pytest.param(sign.b64url(b"short"), id="5-bytes"),
        pytest.param(sign.b64url(bytes(32)) + "=", id="padded"),
        pytest.param("A" * 9000, id="oversized-file"),
        pytest.param(b"\xff\xfe\x00", id="not-text"),
    ],
)
def test_the_loader_refuses_a_missing_empty_undecodable_or_short_key(
    tmp_path: Any, content: Any
) -> None:
    if content is not None:
        _write_key(tmp_path, content)
    with pytest.raises(wa.AssignmentError) as raised:
        wa.load_assignment_key(tmp_path)
    assert raised.value.reason == "key_unavailable"


def test_the_loader_refuses_a_directory_in_place_of_the_file(tmp_path: Any) -> None:
    wa.assignment_key_path(tmp_path).mkdir(parents=True)
    with pytest.raises(wa.AssignmentError) as raised:
        wa.load_assignment_key(tmp_path)
    assert raised.value.reason == "key_unavailable"


def test_the_loader_accepts_a_32_byte_key_and_a_longer_one(tmp_path: Any) -> None:
    raw = bytes(range(32))
    _write_key(tmp_path, sign.b64url(raw) + "\n")
    assert wa.load_assignment_key(tmp_path) == raw
    longer = bytes(range(48))
    _write_key(tmp_path, sign.b64url(longer))
    assert wa.load_assignment_key(tmp_path) == longer


def test_the_loader_reads_the_file_on_every_call(tmp_path: Any) -> None:
    first, second = bytes(range(32)), bytes(range(64, 96))
    _write_key(tmp_path, sign.b64url(first))
    token = sign.assignment(first, exp=FUTURE)
    assert _verify(token, key=wa.load_assignment_key(tmp_path))
    _write_key(tmp_path, sign.b64url(second))  # rotation: the next call sees the new key
    assert wa.load_assignment_key(tmp_path) == second
    assert _refusal(token, key=wa.load_assignment_key(tmp_path)) == "bad_signature"


def test_a_generated_key_is_32_random_bytes_as_url_safe_text_the_loader_decodes(
    tmp_path: Any,
) -> None:
    first, second = wa.generate_assignment_key(), wa.generate_assignment_key()
    assert first != second
    assert isinstance(first, str)
    assert set(first) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-")
    _write_key(tmp_path, first)
    assert len(wa.load_assignment_key(tmp_path)) == 32


def test_the_module_never_touches_the_environment() -> None:
    import verdict.worker_assignment as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    assert "environ" not in source
    assert "getenv" not in source
