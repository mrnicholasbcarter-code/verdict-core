from __future__ import annotations

import json
from pathlib import Path

import pytest

from verdict.free_tier_admit import REASON_METADATA_GHOST, REASON_OPAQUE_ROUTE_DISALLOWED
from verdict.orchestration.opencode_denial import (
    PERMANENT_DENIAL_CODES,
    DenialWatch,
    denial_code_in_text,
    serve_denial_code,
)

FIXTURES = Path(__file__).parent / "fixtures" / "opencode_stdout"
ROUTE = "cc/example-model"


def _retry(message: str, status: int = 503, *, body: str | None = None) -> str:
    error: dict[str, object] = {"type": "provider.internal", "status": status}
    if message:
        error["message"] = message
    if body is not None:
        error["response"] = {"body": body}
    return json.dumps({"type": "retry", "attempt": 2, "error": error})


def _denial(code: str, route: str = ROUTE) -> str:
    return f"assigned route '{route}' denied: {code}"


def _line(text: str) -> bytes:
    return text.encode() + b"\n"


def test_permanent_codes_come_from_free_tier_admit() -> None:
    assert isinstance(PERMANENT_DENIAL_CODES, frozenset)
    assert {REASON_OPAQUE_ROUTE_DISALLOWED, REASON_METADATA_GHOST} == PERMANENT_DENIAL_CODES


def test_code_from_message_str_and_bytes() -> None:
    line = _retry(_denial("opaque_route_disallowed") + " (why)")
    assert serve_denial_code(line, ROUTE) == "opaque_route_disallowed"
    assert serve_denial_code(line.encode(), ROUTE) == "opaque_route_disallowed"


def test_code_from_response_body() -> None:
    body = json.dumps({"error": {"message": _denial("rate_limited_x1")}})
    line = _retry("upstream", body=body)
    assert serve_denial_code(line, ROUTE) == "rate_limited_x1"


@pytest.mark.parametrize(
    "line",
    [
        "not json",
        "",
        "[1, 2]",
        '"assigned route"',
        json.dumps({"type": "error", "error": {"status": 503, "message": _denial("a")}}),
        _retry(_denial("a"), status=500),
        _retry(_denial("a"), status=403),
        _retry("fake upstream 503"),
        _retry(_denial("a", "cc/other-model")),
        _retry(_denial("a", "cc/example-modelx")),
        _retry("assigned route 'cc/example-model' denied: UPPER"),
        json.dumps({"type": "retry", "error": "boom"}),
        json.dumps({"type": "retry"}),
    ],
)
def test_non_denials_return_none(line: str) -> None:
    assert serve_denial_code(line, ROUTE) is None


def test_route_id_is_escaped() -> None:
    line = _retry(_denial("a", "cc/exampleXmodel"))
    assert serve_denial_code(line, "cc/example.model") is None
    assert serve_denial_code(_retry(_denial("a", "cc/example.model")), "cc/example.model") == "a"


@pytest.mark.parametrize("route", ["cc/it's-model", "cc/back\\slash", 'cc/say"hi"'])
def test_route_id_is_quoted_the_way_serve_quotes_it(route: str) -> None:
    """``IntelligenceService._assigned_denial_reason`` writes the route with ``!r``."""
    serve_text = f"assigned route {route!r} denied: opaque_route_disallowed (why)"
    assert serve_denial_code(_retry(serve_text), route) == "opaque_route_disallowed"
    watch = DenialWatch(route)
    assert watch.feed(_line(_retry(serve_text))) == (
        f"assigned route {route!r} denied: opaque_route_disallowed"
    )


@pytest.mark.parametrize("tail", ["X", "_X", "Xy"])
def test_a_capital_straight_after_the_code_means_it_is_not_a_code(tail: str) -> None:
    """Without the boundary the lower-case run would be read as a shorter, different code."""
    assert serve_denial_code(_retry(_denial("opaque_route_disallowed" + tail)), ROUTE) is None


@pytest.mark.parametrize("after", ["", " (why)", '"}', '\\"}'])
def test_a_code_ends_at_the_end_serves_detail_or_the_json_quoting(after: str) -> None:
    text = _denial("a_b") + after
    assert serve_denial_code(_retry(text), ROUTE) == "a_b"
    assert serve_denial_code(_retry("upstream", body=text), ROUTE) == "a_b"


@pytest.mark.parametrize("code", sorted(PERMANENT_DENIAL_CODES))
def test_permanent_code_ends_on_first_denial(code: str) -> None:
    watch = DenialWatch(ROUTE)
    assert watch.feed(_line(_retry(_denial(code)))) == _denial(code)
    assert watch.feed(_line(_retry(_denial(code)))) is None


def test_other_code_ends_on_second_denial_only() -> None:
    watch = DenialWatch(ROUTE)
    line = _line(_retry(_denial("rate_limited")))
    assert watch.feed(line) is None
    assert watch.feed(line) == _denial("rate_limited")
    assert watch.feed(line) is None


def test_other_codes_are_counted_separately() -> None:
    watch = DenialWatch(ROUTE)
    assert watch.feed(_line(_retry(_denial("code_a")))) is None
    assert watch.feed(_line(_retry(_denial("code_b")))) is None
    assert watch.feed(_line(_retry(_denial("code_a")))) == _denial("code_a")


def test_two_denials_in_one_chunk() -> None:
    watch = DenialWatch(ROUTE)
    chunk = _line(_retry(_denial("code_a"))) * 2
    assert watch.feed(chunk) == _denial("code_a")


def test_line_split_across_chunks() -> None:
    watch = DenialWatch(ROUTE)
    data = _line(_retry(_denial(REASON_METADATA_GHOST)))
    for i in range(0, len(data) - 1):
        assert watch.feed(data[i : i + 1]) is None
    assert watch.feed(data[-1:]) == _denial(REASON_METADATA_GHOST)


def test_partial_last_line_stays_buffered() -> None:
    watch = DenialWatch(ROUTE)
    data = _line(_retry(_denial(REASON_METADATA_GHOST)))
    assert watch.feed(data[:-1]) is None
    assert watch.feed(b"\n") == _denial(REASON_METADATA_GHOST)


def test_oversized_partial_line_is_discarded() -> None:
    watch = DenialWatch(ROUTE)
    assert watch.feed(b"x" * (1024 * 1024 + 1)) is None
    assert watch.feed(b"tail\n") is None
    assert watch.feed(_line(_retry(_denial(REASON_OPAQUE_ROUTE_DISALLOWED)))) == _denial(
        REASON_OPAQUE_ROUTE_DISALLOWED
    )


def test_partial_line_of_exactly_one_mib_is_kept() -> None:
    watch = DenialWatch(ROUTE)
    prefix = _retry(_denial(REASON_METADATA_GHOST))[:-1]
    padding = " " * (1024 * 1024 - len(prefix) - 1)
    assert watch.feed(prefix.encode() + padding.encode()) is None
    assert watch.feed(b"}\n") == _denial(REASON_METADATA_GHOST)


def test_denial_for_another_route_is_ignored() -> None:
    watch = DenialWatch(ROUTE)
    line = _line(_retry(_denial(REASON_METADATA_GHOST, "cc/other-model")))
    assert watch.feed(line) is None
    assert watch.feed(line) is None


def _feed_fixture(name: str, route: str) -> list[str]:
    watch = DenialWatch(route)
    found = []
    for raw in (FIXTURES / name).read_bytes().splitlines(keepends=True):
        text = watch.feed(raw)
        if text is not None:
            found.append(text)
    return found


def test_plain_503_fixture_gives_no_denial() -> None:
    assert _feed_fixture("v2_3_2_error_503.jsonl", ROUTE) == []


def test_serve_denial_fixture_gives_one_denial() -> None:
    assert _feed_fixture("v2_3_2_retry_503_serve_denial.jsonl", ROUTE) == [
        _denial("opaque_route_disallowed")
    ]


def test_serve_denial_fixture_shape() -> None:
    lines = (FIXTURES / "v2_3_2_retry_503_serve_denial.jsonl").read_text().splitlines()
    retries = [e for e in map(json.loads, lines) if e["type"] == "retry"]
    assert len(retries) == 2
    for event in retries:
        assert event["error"]["status"] == 503
        assert event["error"]["message"] == (
            "assigned route 'cc/example-model' denied: opaque_route_disallowed "
            "(opaque/combo route is not a concrete auditable identity)"
        )
    assert serve_denial_code(lines[1], ROUTE) == "opaque_route_disallowed"
    assert _feed_fixture("v2_3_2_retry_503_serve_denial.jsonl", "cc/other") == []


def test_denial_code_in_text_matches_own_route_and_empty_route_is_none() -> None:
    text = _denial("opaque_route_disallowed") + " (why)"
    assert denial_code_in_text(text, ROUTE) == "opaque_route_disallowed"
    assert denial_code_in_text(text, "cc/other") is None
    assert denial_code_in_text(text, "") is None
