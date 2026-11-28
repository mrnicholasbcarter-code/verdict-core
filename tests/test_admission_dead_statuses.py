"""Tests that deactivated and credits_exhausted connection statuses are rejected."""

from __future__ import annotations

import pytest

from tests.test_admission import NOW, conn, row
from verdict.admission import RuntimeEvidence, admit


@pytest.mark.parametrize(
    "status", ["deactivated", "credits_exhausted", "Deactivated", " CREDITS_EXHAUSTED "]
)
def test_dead_status_rejects_route(status: str) -> None:
    admitted = admit([row("aa/x")], [conn("aa", status=status)], RuntimeEvidence(), now=NOW)
    assert admitted.ids == frozenset()
    assert {r.route_id: r.reason for r in admitted.records} == {
        "aa/x": "connection_status:" + status.strip().lower()
    }


@pytest.mark.parametrize("status", ["deactivated", "credits_exhausted"])
def test_dead_status_with_ok_connection_does_not_reject(status: str) -> None:
    admitted = admit(
        [row("aa/x")],
        [conn("aa", status=status), conn("aa", status="ok")],
        RuntimeEvidence(),
        now=NOW,
    )
    records = {r.route_id: r.reason for r in admitted.records}
    assert not records.get("aa/x", "").startswith("connection_status:")
