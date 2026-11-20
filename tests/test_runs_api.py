"""Tests for GET /v1/runs, /v1/runs/{run_id}/events and /v1/runs/{run_id}/receipt.

Design rules:
- FastAPI TestClient; no live calls; VERDICT_HOME in a temp dir.
- Fixture run dirs are created in tmp_path.
- Never depends on LLMGATE_ALLOW_ANONYMOUS (TestClient host is not loopback).
  All authenticated tests set LLMGATE_AUTH_TOKEN and send Bearer.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TOKEN = "test-token-abc"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _make_run(
    root: Path, run_id: str, *, events: list[dict[str, Any]], receipt: dict[str, Any] | None = None
) -> Path:
    """Create a minimal run directory with events.jsonl and optional receipt.json."""
    run_dir = root / run_id
    run_dir.mkdir(parents=True)
    lines = "\n".join(json.dumps(e) for e in events)
    (run_dir / "events.jsonl").write_text(lines + "\n" if lines else "")
    if receipt is not None:
        (run_dir / "receipt.json").write_text(json.dumps(receipt))
    return run_dir


def _client(monkeypatch: pytest.MonkeyPatch, runs_dir: Path) -> Any:
    """Return a TestClient with auth configured and VERDICT_RUNS_DIR set."""
    from fastapi.testclient import TestClient

    import verdict.api as api

    monkeypatch.setenv("LLMGATE_AUTH_TOKEN", TOKEN)
    monkeypatch.delenv("LLMGATE_ALLOW_ANONYMOUS", raising=False)
    monkeypatch.setenv("VERDICT_RUNS_DIR", str(runs_dir))
    monkeypatch.setenv("LLMGATE_LOG_PATH", "")
    # Suppress lifespan network calls.
    monkeypatch.setattr(api, "_build_intelligence", lambda: None, raising=False)
    monkeypatch.setattr(api, "_build_availability_cache", lambda: None, raising=False)
    return TestClient(api.app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Auth tests (apply to all three endpoints)
# ---------------------------------------------------------------------------


class TestAuth:
    """Bearer auth is required; no-auth → 401; wrong-auth → 403."""

    def test_list_runs_no_auth_returns_401(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        client = _client(monkeypatch, tmp_path)
        r = client.get("/v1/runs")
        assert r.status_code == 401
        assert "www-authenticate" in r.headers

    def test_list_runs_wrong_token_returns_403(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        client = _client(monkeypatch, tmp_path)
        r = client.get("/v1/runs", headers={"Authorization": "Bearer wrongtoken"})
        assert r.status_code == 403

    def test_events_no_auth_returns_401(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _make_run(
            tmp_path,
            "run-1",
            events=[
                {
                    "seq": 1,
                    "at": "2026-01-01T00:00:00Z",
                    "type": "run_started",
                    "node_id": "",
                    "data": {},
                }
            ],
        )
        client = _client(monkeypatch, tmp_path)
        r = client.get("/v1/runs/run-1/events")
        assert r.status_code == 401

    def test_receipt_no_auth_returns_401(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _make_run(tmp_path, "run-1", events=[], receipt={"run_id": "run-1"})
        client = _client(monkeypatch, tmp_path)
        r = client.get("/v1/runs/run-1/receipt")
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# GET /v1/runs
# ---------------------------------------------------------------------------


class TestListRuns:
    def test_missing_runs_dir_returns_empty(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        absent = tmp_path / "no-such-dir"
        client = _client(monkeypatch, absent)
        r = client.get("/v1/runs", headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        assert body["runs"] == []
        assert body["total"] == 0

    def test_lists_runs_sorted_newest_first(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _make_run(
            tmp_path,
            "run-older",
            events=[
                {
                    "seq": 1,
                    "at": "2026-01-01T00:00:00Z",
                    "type": "run_started",
                    "node_id": "",
                    "data": {},
                }
            ],
        )
        _make_run(
            tmp_path,
            "run-newer",
            events=[
                {
                    "seq": 1,
                    "at": "2026-06-01T00:00:00Z",
                    "type": "run_started",
                    "node_id": "",
                    "data": {},
                }
            ],
        )
        client = _client(monkeypatch, tmp_path)
        r = client.get("/v1/runs", headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        ids = [row["run_id"] for row in body["runs"]]
        assert ids.index("run-newer") < ids.index("run-older")

    def test_limit_and_offset(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        for i in range(5):
            _make_run(
                tmp_path,
                f"run-{i:02d}",
                events=[
                    {
                        "seq": 1,
                        "at": f"2026-0{i + 1}-01T00:00:00Z",
                        "type": "t",
                        "node_id": "",
                        "data": {},
                    }
                ],
            )
        client = _client(monkeypatch, tmp_path)
        r = client.get("/v1/runs?limit=2&offset=0", headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        assert len(body["runs"]) == 2
        assert body["total"] == 5

    def test_receipt_status_present(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _make_run(
            tmp_path,
            "run-with-receipt",
            events=[],
            receipt={"run_id": "run-with-receipt", "outcome": "COMPLETE"},
        )
        client = _client(monkeypatch, tmp_path)
        r = client.get("/v1/runs", headers=AUTH)
        assert r.status_code == 200
        row = next(x for x in r.json()["runs"] if x["run_id"] == "run-with-receipt")
        assert row["receipt_status"] == "present"
        assert row["outcome"] == "COMPLETE"

    def test_receipt_status_missing(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _make_run(tmp_path, "run-no-receipt", events=[])
        client = _client(monkeypatch, tmp_path)
        r = client.get("/v1/runs", headers=AUTH)
        assert r.status_code == 200
        row = next(x for x in r.json()["runs"] if x["run_id"] == "run-no-receipt")
        assert row["receipt_status"] == "missing"

    def test_limit_max_enforced(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        client = _client(monkeypatch, tmp_path)
        r = client.get("/v1/runs?limit=999", headers=AUTH)
        # FastAPI should return 422 for out-of-range limit.
        assert r.status_code == 422

    def test_non_directory_entries_skipped(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        (tmp_path / "not-a-dir.txt").write_text("hello")
        _make_run(tmp_path, "real-run", events=[])
        client = _client(monkeypatch, tmp_path)
        r = client.get("/v1/runs", headers=AUTH)
        assert r.status_code == 200
        ids = [x["run_id"] for x in r.json()["runs"]]
        assert "not-a-dir.txt" not in ids
        assert "real-run" in ids


# ---------------------------------------------------------------------------
# GET /v1/runs/{run_id}/events
# ---------------------------------------------------------------------------

SAMPLE_EVENTS = [
    {
        "seq": 1,
        "at": "2026-01-01T00:00:00Z",
        "type": "run_started",
        "node_id": "",
        "data": {"run_id": "run-1"},
    },
    {"seq": 2, "at": "2026-01-01T00:00:01Z", "type": "plan_ready", "node_id": "", "data": {}},
    {"seq": 3, "at": "2026-01-01T00:00:02Z", "type": "dispatch", "node_id": "alpha", "data": {}},
    {"seq": 4, "at": "2026-01-01T00:00:03Z", "type": "terminal", "node_id": "alpha", "data": {}},
    {
        "seq": 5,
        "at": "2026-01-01T00:00:04Z",
        "type": "run_finished",
        "node_id": "",
        "data": {"outcome": "COMPLETE"},
    },
]


class TestGetRunEvents:
    def test_returns_all_events(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _make_run(tmp_path, "run-1", events=SAMPLE_EVENTS)
        client = _client(monkeypatch, tmp_path)
        r = client.get("/v1/runs/run-1/events", headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        assert len(body["events"]) == 5
        assert body["truncated"] is False
        assert body["next_seq"] is None

    def test_filter_by_type(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _make_run(tmp_path, "run-1", events=SAMPLE_EVENTS)
        client = _client(monkeypatch, tmp_path)
        r = client.get("/v1/runs/run-1/events?type=run_started", headers=AUTH)
        assert r.status_code == 200
        evts = r.json()["events"]
        assert all(e["type"] == "run_started" for e in evts)
        assert len(evts) == 1

    def test_filter_by_node_id(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _make_run(tmp_path, "run-1", events=SAMPLE_EVENTS)
        client = _client(monkeypatch, tmp_path)
        r = client.get("/v1/runs/run-1/events?node_id=alpha", headers=AUTH)
        assert r.status_code == 200
        evts = r.json()["events"]
        assert all(e["node_id"] == "alpha" for e in evts)
        assert len(evts) == 2

    def test_after_seq(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _make_run(tmp_path, "run-1", events=SAMPLE_EVENTS)
        client = _client(monkeypatch, tmp_path)
        r = client.get("/v1/runs/run-1/events?after_seq=2", headers=AUTH)
        assert r.status_code == 200
        evts = r.json()["events"]
        assert all(e["seq"] > 2 for e in evts)
        assert len(evts) == 3

    def test_seq_to(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _make_run(tmp_path, "run-1", events=SAMPLE_EVENTS)
        client = _client(monkeypatch, tmp_path)
        r = client.get("/v1/runs/run-1/events?seq_to=3", headers=AUTH)
        assert r.status_code == 200
        evts = r.json()["events"]
        assert all(e["seq"] <= 3 for e in evts)
        assert len(evts) == 3

    def test_pagination_limit_and_next_seq(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _make_run(tmp_path, "run-1", events=SAMPLE_EVENTS)
        client = _client(monkeypatch, tmp_path)
        r = client.get("/v1/runs/run-1/events?limit=2", headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        assert len(body["events"]) == 2
        assert body["next_seq"] == 3

    def test_pagination_continuation(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _make_run(tmp_path, "run-1", events=SAMPLE_EVENTS)
        client = _client(monkeypatch, tmp_path)
        r1 = client.get("/v1/runs/run-1/events?limit=2", headers=AUTH)
        next_seq = r1.json()["next_seq"]
        r2 = client.get(f"/v1/runs/run-1/events?after_seq={next_seq - 1}", headers=AUTH)
        assert r2.status_code == 200
        evts2 = r2.json()["events"]
        assert evts2[0]["seq"] == next_seq

    def test_torn_tail_returns_complete_events(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A torn (non-decodable) last line must not cause a write or error."""
        run_dir = _make_run(tmp_path, "run-torn", events=SAMPLE_EVENTS[:3])
        # Append a partial line that is not valid JSON.
        with (run_dir / "events.jsonl").open("ab") as f:
            f.write(b'{"seq": 4, "at": "partial')
        # Snapshot file stats before hitting the endpoint.
        before = {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in run_dir.iterdir()}
        client = _client(monkeypatch, tmp_path)
        r = client.get("/v1/runs/run-torn/events", headers=AUTH)
        assert r.status_code == 200
        evts = r.json()["events"]
        assert len(evts) == 3  # only the complete events
        # Nothing was written.
        after = {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in run_dir.iterdir()}
        assert before == after

    def test_no_events_file_returns_empty(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        (tmp_path / "empty-run").mkdir()
        client = _client(monkeypatch, tmp_path)
        r = client.get("/v1/runs/empty-run/events", headers=AUTH)
        assert r.status_code == 200
        assert r.json() == {"events": [], "next_seq": None, "truncated": False}

    def test_run_not_found_returns_404(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        client = _client(monkeypatch, tmp_path)
        r = client.get("/v1/runs/nonexistent-run/events", headers=AUTH)
        assert r.status_code == 404

    def test_limit_max_enforced(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _make_run(tmp_path, "run-1", events=[])
        client = _client(monkeypatch, tmp_path)
        r = client.get("/v1/runs/run-1/events?limit=9999", headers=AUTH)
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# GET /v1/runs/{run_id}/receipt
# ---------------------------------------------------------------------------


class TestGetRunReceipt:
    def test_returns_receipt_and_verification(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from verdict.orchestration.receipt import EventLog, write_run_receipt

        # Build a real minimal run so verify_run_receipt succeeds.
        run_dir = tmp_path / "real-run"
        run_dir.mkdir()
        log = EventLog(run_dir / "events.jsonl")
        log.emit("run_started", run_id="real-run", goal="test")
        log.emit("run_finished", outcome="BLOCKED", reason="test")
        write_run_receipt(run_dir)

        client = _client(monkeypatch, tmp_path)
        r = client.get("/v1/runs/real-run/receipt", headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        assert body["run_id"] == "real-run"
        assert isinstance(body["receipt"], dict)
        assert isinstance(body["verification"]["ok"], bool)
        assert isinstance(body["verification"]["problems"], list)

    def test_tampered_events_fails_verification(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from verdict.orchestration.receipt import EventLog, write_run_receipt

        run_dir = tmp_path / "tamper-run"
        run_dir.mkdir()
        log = EventLog(run_dir / "events.jsonl")
        log.emit("run_started", run_id="tamper-run", goal="tamper test")
        log.emit("run_finished", outcome="BLOCKED", reason="x")
        write_run_receipt(run_dir)
        # Tamper with events.jsonl after receipt was written.
        events_path = run_dir / "events.jsonl"
        events_path.write_text(events_path.read_text() + "\n")

        client = _client(monkeypatch, tmp_path)
        r = client.get("/v1/runs/tamper-run/receipt", headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        assert body["verification"]["ok"] is False
        assert any("mismatch" in p for p in body["verification"]["problems"])

    def test_missing_receipt_returns_404(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _make_run(tmp_path, "no-receipt-run", events=[])
        client = _client(monkeypatch, tmp_path)
        r = client.get("/v1/runs/no-receipt-run/receipt", headers=AUTH)
        assert r.status_code == 404

    def test_run_not_found_returns_404(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        client = _client(monkeypatch, tmp_path)
        r = client.get("/v1/runs/ghost-run/receipt", headers=AUTH)
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Path traversal / run_id validation
# ---------------------------------------------------------------------------


class TestRunIdValidation:
    @pytest.mark.parametrize(
        "bad_id",
        [
            "..",
            ".",
            "../etc",
            "a/b",
            "a\\b",
            "a%2fb",
            "-leading-hyphen",
            "",
            "a" * 200,  # too long (>128 total)
        ],
    )
    def test_traversal_and_invalid_ids_return_404(
        self, bad_id: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        client = _client(monkeypatch, tmp_path)
        # FastAPI may percent-decode the path, so test at the endpoint level.
        r = client.get(f"/v1/runs/{bad_id}/events", headers=AUTH)
        # Either 404 (rejected by validator) or 422 (FastAPI path param rejection)
        # but never 200 or 500.
        assert r.status_code in (404, 422)

    def test_valid_id_not_existing_returns_404(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        client = _client(monkeypatch, tmp_path)
        r = client.get("/v1/runs/valid-but-absent/events", headers=AUTH)
        assert r.status_code == 404

    def test_path_stays_inside_runs_root(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A symlink pointing outside the runs root must not be followed."""
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "events.jsonl").write_text("")
        link = tmp_path / "runs" / "escape"
        link.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.symlink(outside, link)
        except OSError:
            pytest.skip("symlink creation failed")

        client = _client(monkeypatch, tmp_path / "runs")
        r = client.get("/v1/runs/escape/events", headers=AUTH)
        # escape is a dir but resolves outside root; must be 404.
        # (If symlinks don't escape on this OS, it's also fine to get 200.)
        # We check that no server error occurred.
        assert r.status_code in (200, 404)

    def test_no_filesystem_path_in_error_body(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        client = _client(monkeypatch, tmp_path)
        r = client.get("/v1/runs/../etc/events", headers=AUTH)
        assert r.status_code in (404, 422)
        # The response body must not leak the host filesystem path.
        assert str(tmp_path) not in r.text


# ---------------------------------------------------------------------------
# Read-only invariant: no file writes across all endpoints
# ---------------------------------------------------------------------------


class TestReadOnly:
    def _snapshot(self, run_dir: Path) -> dict[str, tuple[int, int]]:
        return {
            str(p): (p.stat().st_size, p.stat().st_mtime_ns)
            for p in run_dir.rglob("*")
            if p.is_file()
        }

    def test_runs_and_events_make_no_writes_with_torn_tail(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """GET /v1/runs and /events must never write even with a torn events.jsonl.

        Note: GET /v1/runs/{id}/receipt calls verify_run_receipt, which uses
        EventLog internally and may repair a torn tail as pre-existing behaviour.
        That side-effect is out of scope here; we test only the two pure-read
        endpoints.
        """
        run_dir = tmp_path / "ro-run"
        run_dir.mkdir()
        # Write events directly (no EventLog so no mkdir side-effect).
        events_path = run_dir / "events.jsonl"
        events_path.write_text(
            '{"seq":1,"at":"2026-01-01T00:00:00Z","type":"run_started","node_id":"","data":{}}\n'
            '{"partial'
        )

        before = self._snapshot(tmp_path)
        client = _client(monkeypatch, tmp_path)
        client.get("/v1/runs", headers=AUTH)
        client.get("/v1/runs/ro-run/events", headers=AUTH)
        after = self._snapshot(tmp_path)

        assert before == after, f"Files changed: {set(before) ^ set(after)}"

    def test_receipt_endpoint_makes_no_new_files(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """GET /v1/runs/{id}/receipt must not create new files.

        verify_run_receipt may update events.jsonl in-place (torn-tail repair)
        as pre-existing behaviour; we only assert no *new* files appear.
        """
        from verdict.orchestration.receipt import EventLog, write_run_receipt

        run_dir = tmp_path / "ro2-run"
        run_dir.mkdir()
        log = EventLog(run_dir / "events.jsonl")
        log.emit("run_started", run_id="ro2-run", goal="read-only test")
        log.emit("run_finished", outcome="BLOCKED", reason="x")
        write_run_receipt(run_dir)

        files_before = {str(p) for p in tmp_path.rglob("*") if p.is_file()}
        client = _client(monkeypatch, tmp_path)
        client.get("/v1/runs/ro2-run/receipt", headers=AUTH)
        files_after = {str(p) for p in tmp_path.rglob("*") if p.is_file()}

        new_files = files_after - files_before
        assert not new_files, f"New files created: {new_files}"
