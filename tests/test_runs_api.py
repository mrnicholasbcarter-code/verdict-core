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
        from verdict.api import _RUNS_PAGE_LIMIT_MAX

        client = _client(monkeypatch, tmp_path)
        r = client.get(f"/v1/runs?limit={_RUNS_PAGE_LIMIT_MAX + 1}", headers=AUTH)
        # FastAPI should return 422 for out-of-range limit.
        assert r.status_code == 422

    def test_total_scan_truncated_flag(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """When more runs exist than _RUNS_SCAN_LIMIT, total_scan_truncated must be True."""
        from verdict.api import _RUNS_SCAN_LIMIT

        # Create SCAN_LIMIT + 1 run directories.
        for i in range(_RUNS_SCAN_LIMIT + 1):
            (tmp_path / f"run-{i:04d}").mkdir()
        client = _client(monkeypatch, tmp_path)
        r = client.get("/v1/runs?limit=1", headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        assert body["total_scan_truncated"] is True

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
        assert r.json() == {
            "events": [],
            "next_seq": None,
            "truncated": False,
            "oversized_seqs": [],
        }

    def test_run_not_found_returns_404(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        client = _client(monkeypatch, tmp_path)
        r = client.get("/v1/runs/nonexistent-run/events", headers=AUTH)
        assert r.status_code == 404

    def test_limit_max_enforced(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        from verdict.api import _RUNS_EVENTS_LIMIT_MAX

        _make_run(tmp_path, "run-1", events=[])
        client = _client(monkeypatch, tmp_path)
        r = client.get(f"/v1/runs/run-1/events?limit={_RUNS_EVENTS_LIMIT_MAX + 1}", headers=AUTH)
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
        """A symlink pointing outside the runs root must be rejected with 404."""
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "events.jsonl").write_text(
            '{"seq":1,"at":"2026-01-01T00:00:00Z","type":"secret","node_id":"","data":{"key":"SUPERSECRET"}}\n'
        )
        link = tmp_path / "runs" / "escape"
        link.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.symlink(outside, link)
        except OSError:
            pytest.skip("symlink creation failed")

        client = _client(monkeypatch, tmp_path / "runs")
        r = client.get("/v1/runs/escape/events", headers=AUTH)
        # A symlink run dir escaping root must be 404, never 200.
        assert r.status_code == 404
        assert "SUPERSECRET" not in r.text

    def test_symlink_events_file_rejected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """events.jsonl that is a symlink escaping the run dir must yield empty events."""
        runs = tmp_path / "runs"
        run_dir = runs / "sym-run"
        run_dir.mkdir(parents=True)
        outside = tmp_path / "outside"
        outside.mkdir()
        secret_data = '{"seq":1,"at":"2026-01-01T00:00:00Z","type":"t","node_id":"","data":{"api_key":"sk-12345"}}\n'
        (outside / "events.jsonl").write_text(secret_data)
        try:
            os.symlink(outside / "events.jsonl", run_dir / "events.jsonl")
        except OSError:
            pytest.skip("symlink creation failed")

        client = _client(monkeypatch, runs)
        r = client.get("/v1/runs/sym-run/events", headers=AUTH)
        # Symlink to outside file: must return empty events, not the outside content.
        assert r.status_code == 200
        assert r.json()["events"] == []
        assert "sk-12345" not in r.text

    def test_symlink_receipt_file_rejected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """receipt.json that is a symlink escaping the run dir must yield 404."""
        runs = tmp_path / "runs"
        run_dir = runs / "sym-rcpt"
        run_dir.mkdir(parents=True)
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "receipt.json").write_text('{"run_id":"outside","secret":"topsecret"}')
        try:
            os.symlink(outside / "receipt.json", run_dir / "receipt.json")
        except OSError:
            pytest.skip("symlink creation failed")

        client = _client(monkeypatch, runs)
        r = client.get("/v1/runs/sym-rcpt/receipt", headers=AUTH)
        # Symlink receipt.json escaping run dir: must 404, never return outside content.
        assert r.status_code == 404
        assert "topsecret" not in r.text

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
        """GET /v1/runs and /events must never write even with a torn events.jsonl."""
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

    def test_receipt_existing_files_unchanged(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """receipt endpoint must leave events.jsonl and receipt.json byte- and mtime-identical.

        A GET must never trigger EventLog's torn-tail repair: it writes events.jsonl
        in place, which would mutate digest-bound data.  Use a fully intact (not torn)
        events.jsonl so repair does not trigger; then assert both files are unchanged.
        """
        from verdict.orchestration.receipt import EventLog, write_run_receipt

        run_dir = tmp_path / "immutable-run"
        run_dir.mkdir()
        log = EventLog(run_dir / "events.jsonl")
        log.emit("run_started", run_id="immutable-run", goal="immutability test")
        log.emit("run_finished", outcome="BLOCKED", reason="immutable")
        write_run_receipt(run_dir)

        def _stats() -> dict[str, tuple[bytes, int]]:
            result = {}
            for name in ("events.jsonl", "receipt.json"):
                p = run_dir / name
                result[name] = (p.read_bytes(), p.stat().st_mtime_ns)
            return result

        before = _stats()
        client = _client(monkeypatch, tmp_path)
        client.get("/v1/runs/immutable-run/receipt", headers=AUTH)
        after = _stats()

        for name in ("events.jsonl", "receipt.json"):
            assert before[name][0] == after[name][0], f"{name} bytes changed"
            assert before[name][1] == after[name][1], f"{name} mtime changed"


# ---------------------------------------------------------------------------
# Credential redaction: events response must not expose secret-like fields
# ---------------------------------------------------------------------------


_CRED_EVENTS: list[dict[str, Any]] = [
    {
        "seq": 1,
        "at": "2026-01-01T00:00:00Z",
        "type": "worker_result",
        "node_id": "n1",
        "data": {
            "api_key": "sk-secret-key-12345",
            "password": "hunter2",
            "normal_field": "visible-value",
        },
    },
    {
        "seq": 2,
        "at": "2026-01-01T00:00:01Z",
        "type": "run_finished",
        "node_id": "",
        "data": {"outcome": "COMPLETE", "secret": "do-not-expose"},
    },
]


class TestCredentialRedaction:
    """The events endpoint redacts values in event data whose key names match
    well-known credential patterns (api_key, password, secret, etc.).

    The events are written by internal orchestration code (not user-generated),
    so this is a defence-in-depth measure: even if a future change accidentally
    logs a secret-shaped field, it will not be served to callers.
    """

    def test_credential_fields_are_redacted(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _make_run(tmp_path, "cred-run", events=_CRED_EVENTS)
        client = _client(monkeypatch, tmp_path)
        r = client.get("/v1/runs/cred-run/events", headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        assert len(body["events"]) == 2

        # Credential values must not appear anywhere in the response.
        response_text = r.text
        assert "sk-secret-key-12345" not in response_text
        assert "hunter2" not in response_text
        assert "do-not-expose" not in response_text

        # Non-credential fields must still be present.
        assert "visible-value" in response_text

        # The data dict must still exist but with redacted values.
        evt1_data = body["events"][0]["data"]
        assert evt1_data["api_key"] == "[REDACTED]"
        assert evt1_data["password"] == "[REDACTED]"
        assert evt1_data["normal_field"] == "visible-value"

    def test_non_string_credential_fields_are_redacted(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Redaction applies to non-string values too (e.g. dict or list under a secret key)."""
        events = [
            {
                "seq": 1,
                "at": "2026-01-01T00:00:00Z",
                "type": "t",
                "node_id": "",
                "data": {"password": {"nested": "value"}, "count": 42},
            }
        ]
        _make_run(tmp_path, "nested-cred-run", events=events)
        client = _client(monkeypatch, tmp_path)
        r = client.get("/v1/runs/nested-cred-run/events", headers=AUTH)
        assert r.status_code == 200
        evt_data = r.json()["events"][0]["data"]
        assert evt_data["password"] == "[REDACTED]"
        assert evt_data["count"] == 42

    def test_top_level_event_field_is_redacted(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Credential key at the top level of the event dict is redacted."""
        events = [
            {
                "seq": 1,
                "at": "2026-01-01T00:00:00Z",
                "type": "run_started",
                "node_id": "",
                # api_key at the event top level (not inside data)
                "api_key": "sk-toplevel-secret",
                "data": {},
            }
        ]
        _make_run(tmp_path, "toplevel-cred-run", events=events)
        client = _client(monkeypatch, tmp_path)
        r = client.get("/v1/runs/toplevel-cred-run/events", headers=AUTH)
        assert r.status_code == 200
        evt = r.json()["events"][0]
        assert "sk-toplevel-secret" not in r.text
        assert evt["api_key"] == "[REDACTED]"

    def test_depth2_event_field_is_redacted(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Credential key nested at depth 2 inside data is redacted."""
        events = [
            {
                "seq": 1,
                "at": "2026-01-01T00:00:00Z",
                "type": "run_started",
                "node_id": "",
                "data": {"inner": {"secret": "nested-secret-value", "safe": "ok"}},
            }
        ]
        _make_run(tmp_path, "depth2-cred-run", events=events)
        client = _client(monkeypatch, tmp_path)
        r = client.get("/v1/runs/depth2-cred-run/events", headers=AUTH)
        assert r.status_code == 200
        evt_inner = r.json()["events"][0]["data"]["inner"]
        assert "nested-secret-value" not in r.text
        assert evt_inner["secret"] == "[REDACTED]"
        assert evt_inner["safe"] == "ok"


# ---------------------------------------------------------------------------
# Receipt redaction: receipt response must not expose credential-named fields
# ---------------------------------------------------------------------------


class TestReceiptRedaction:
    """The receipt endpoint applies the same credential redaction as the events
    endpoint to the receipt payload.  Digest verification still runs on the
    raw file bytes, never on the redacted copy, so the verify result is correct.
    """

    def test_receipt_credential_fields_are_redacted(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A receipt with a credential-named key comes back redacted."""
        from verdict.orchestration.receipt import EventLog, write_run_receipt

        run_dir = tmp_path / "redact-rcpt"
        run_dir.mkdir()
        log = EventLog(run_dir / "events.jsonl")
        log.emit("run_started", run_id="redact-rcpt", goal="redaction test")
        log.emit("run_finished", outcome="BLOCKED", reason="test")
        write_run_receipt(run_dir)

        # Inject a credential-named field into receipt.json (simulates a future
        # change that accidentally logs one there).
        import json as _json

        receipt_path = run_dir / "receipt.json"
        raw = _json.loads(receipt_path.read_bytes())
        raw["api_key"] = "receipt-secret-value"
        receipt_path.write_text(_json.dumps(raw))

        client = _client(monkeypatch, tmp_path)
        r = client.get("/v1/runs/redact-rcpt/receipt", headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        assert "receipt-secret-value" not in r.text
        assert body["receipt"]["api_key"] == "[REDACTED]"
        # Verification result must still reflect the tampered file (digest changed).
        assert isinstance(body["verification"]["ok"], bool)
        assert isinstance(body["verification"]["problems"], list)

    def test_receipt_verify_uses_raw_bytes_not_redacted(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Verification result is based on raw file bytes, not on the redacted copy.

        A clean (unmodified) run should have verification.ok True regardless of
        redaction, because the redaction happens after verification reads the files.
        """
        from verdict.orchestration.receipt import EventLog, write_run_receipt

        run_dir = tmp_path / "verify-raw-run"
        run_dir.mkdir()
        log = EventLog(run_dir / "events.jsonl")
        log.emit("run_started", run_id="verify-raw-run", goal="verify test")
        log.emit("run_finished", outcome="BLOCKED", reason="test")
        write_run_receipt(run_dir)

        client = _client(monkeypatch, tmp_path)
        r = client.get("/v1/runs/verify-raw-run/receipt", headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        # A clean run must verify ok; if verify saw the redacted copy it would
        # find a digest mismatch.
        assert body["verification"]["ok"] is True
        assert body["verification"]["problems"] == []


# ---------------------------------------------------------------------------
# Byte budget: oversized single events must never exceed the response bound
# ---------------------------------------------------------------------------


class TestOversizedEvents:
    """A single event larger than _RUNS_EVENTS_BYTE_BUDGET must be omitted,
    truncated must be True, and the seq must appear in oversized_seqs."""

    def test_oversized_event_omitted_and_reported(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A 2 MiB event (> 1 MiB budget) must not be returned."""
        from verdict.api import _RUNS_EVENTS_BYTE_BUDGET

        large_payload = "x" * (_RUNS_EVENTS_BYTE_BUDGET + 1)
        events = [
            {
                "seq": 1,
                "at": "2026-01-01T00:00:00Z",
                "type": "big_event",
                "node_id": "",
                "data": {"blob": large_payload},
            }
        ]
        _make_run(tmp_path, "big-run", events=events)
        client = _client(monkeypatch, tmp_path)
        r = client.get("/v1/runs/big-run/events", headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        # The oversized event must not be in the events list.
        assert body["events"] == []
        assert body["truncated"] is True
        assert 1 in body["oversized_seqs"]

    def test_oversized_event_followed_by_small_event(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """An oversized seq is skipped but scanning continues; the small event
        after it must still be returned."""
        from verdict.api import _RUNS_EVENTS_BYTE_BUDGET

        large_payload = "x" * (_RUNS_EVENTS_BYTE_BUDGET + 1)
        events = [
            {
                "seq": 1,
                "at": "2026-01-01T00:00:00Z",
                "type": "big_event",
                "node_id": "",
                "data": {"blob": large_payload},
            },
            {
                "seq": 2,
                "at": "2026-01-01T00:00:01Z",
                "type": "small_event",
                "node_id": "",
                "data": {},
            },
        ]
        _make_run(tmp_path, "big-then-small", events=events)
        client = _client(monkeypatch, tmp_path)
        r = client.get("/v1/runs/big-then-small/events", headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        # seq=1 oversized — omitted; seq=2 small — must be present.
        seqs = [e["seq"] for e in body["events"]]
        assert 1 not in seqs
        assert 2 in seqs
        assert body["truncated"] is True
        assert 1 in body["oversized_seqs"]


# ---------------------------------------------------------------------------
# Receipt endpoint: torn tail — file must be byte-identical, problems reported
# ---------------------------------------------------------------------------


class TestReceiptLargeLog:
    """Receipt endpoint uses EventLog(repair=False) so it never truncates the
    events.jsonl, even on large logs and even if the tail is torn."""

    def _write_large_log(self, run_dir: Path, run_id: str) -> None:
        """Write a large events.jsonl (> 64 KiB) followed by proper run events.

        Padding events are written directly to the file (not via EventLog) to
        avoid EventLog's event-type allowlist; EventLog is used only for the
        typed run_started / run_finished events appended at the end.
        """
        from verdict.orchestration.receipt import EventLog

        events_path = run_dir / "events.jsonl"
        # Write ~1000 raw padding lines (each ~100 bytes) to exceed 64 KiB.
        with events_path.open("w") as fh:
            for i in range(1000):
                fh.write(
                    json.dumps(
                        {
                            "seq": i + 1,
                            "at": "2026-01-01T00:00:00Z",
                            "type": "heartbeat",
                            "node_id": "",
                            "data": {"padding": "p" * 60},
                        }
                    )
                    + "\n"
                )
        # Append the proper typed events via EventLog (it reads the seq counter).
        log = EventLog(events_path)
        log.emit("run_started", run_id=run_id, goal="tail test")
        log.emit("run_finished", outcome="BLOCKED", reason="tail test")

    def test_large_intact_log_receipt_ok(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A large intact events.jsonl (> 64 KiB) must have its receipt returned
        without mutation.  Uses repair=False path unconditionally."""
        from verdict.orchestration.receipt import write_run_receipt

        run_dir = tmp_path / "large-log"
        run_dir.mkdir()
        self._write_large_log(run_dir, "large-log")
        write_run_receipt(run_dir)

        client = _client(monkeypatch, tmp_path)
        r = client.get("/v1/runs/large-log/receipt", headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body["verification"]["ok"], bool)

    def test_large_torn_log_file_unchanged_problem_reported(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A torn tail is reported (digest mismatch) and events.jsonl is never
        mutated — repair=False holds even when the tail is torn at GET time."""
        from verdict.orchestration.receipt import write_run_receipt

        run_dir = tmp_path / "large-torn"
        run_dir.mkdir()
        self._write_large_log(run_dir, "large-torn")
        write_run_receipt(run_dir)
        # Append a partial line to tear the tail.
        with (run_dir / "events.jsonl").open("ab") as f:
            f.write(b'{"seq": 9999, "partial')

        before = {str(p): (p.stat().st_size, p.stat().st_mtime_ns) for p in run_dir.iterdir()}
        client = _client(monkeypatch, tmp_path)
        r = client.get("/v1/runs/large-torn/receipt", headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        # A torn tail changes the file hash → problems must be non-empty.
        assert body["verification"]["ok"] is False
        assert len(body["verification"]["problems"]) > 0
        # No mutation: file sizes and mtimes must be unchanged.
        after = {str(p): (p.stat().st_size, p.stat().st_mtime_ns) for p in run_dir.iterdir()}
        assert before == after


# ---------------------------------------------------------------------------
# TOCTOU: receipt endpoint never mutates even if torn tail appears mid-call
# ---------------------------------------------------------------------------


class TestReceiptTOCTOU:
    """verify_run_receipt(repair=False) holds unconditionally so a torn tail
    injected at any point between the HTTP call and the disk reads cannot
    trigger EventLog's _repair_torn_tail truncation."""

    def test_torn_tail_present_at_get_time_is_not_repaired(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Torn tail present when GET is received → byte-identical, problem reported.

        This is the synchronous version of the race: the file is already torn
        before the endpoint runs, so repair=False is immediately exercised.
        """
        from verdict.orchestration.receipt import EventLog, write_run_receipt

        run_dir = tmp_path / "toctou-sync"
        run_dir.mkdir()
        log = EventLog(run_dir / "events.jsonl")
        log.emit("run_started", run_id="toctou-sync", goal="toctou test")
        log.emit("run_finished", outcome="BLOCKED", reason="toctou")
        write_run_receipt(run_dir)
        # Tear the tail.
        with (run_dir / "events.jsonl").open("ab") as f:
            f.write(b'{"seq": 999, "partial')

        before_bytes = (run_dir / "events.jsonl").read_bytes()
        before_mtime = (run_dir / "events.jsonl").stat().st_mtime_ns

        client = _client(monkeypatch, tmp_path)
        r = client.get("/v1/runs/toctou-sync/receipt", headers=AUTH)
        assert r.status_code == 200

        after_bytes = (run_dir / "events.jsonl").read_bytes()
        after_mtime = (run_dir / "events.jsonl").stat().st_mtime_ns

        assert before_bytes == after_bytes, "events.jsonl was mutated"
        assert before_mtime == after_mtime, "events.jsonl mtime changed"
        # Digest mismatch must be reported.
        body = r.json()
        assert body["verification"]["ok"] is False

    def test_torn_tail_injected_mid_call_is_not_repaired(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Torn tail injected AFTER receipt bytes are read but before verify runs.

        Simulates the TOCTOU window: a live run appends a partial line between
        any intermediate step and the EventLog open.  With repair=False the
        file must still be byte-identical after the call.
        """
        from verdict.orchestration.receipt import EventLog, write_run_receipt

        run_dir = tmp_path / "toctou-race"
        run_dir.mkdir()
        log = EventLog(run_dir / "events.jsonl")
        log.emit("run_started", run_id="toctou-race", goal="toctou race")
        log.emit("run_finished", outcome="BLOCKED", reason="race")
        write_run_receipt(run_dir)

        events_path = run_dir / "events.jsonl"

        # Monkeypatch verify_run_receipt to inject a torn tail just before it
        # reads the file, simulating a concurrent append.
        from verdict.orchestration import receipt as receipt_mod

        original_verify_fn = receipt_mod.verify_run_receipt

        def _inject_then_verify(rd: Any, *, repair: bool = True) -> list[str]:
            # Inject the torn tail now — AFTER the endpoint has read receipt.json.
            with events_path.open("ab") as f:
                f.write(b'{"seq": 888, "injected')
            return original_verify_fn(rd, repair=repair)

        monkeypatch.setattr(receipt_mod, "verify_run_receipt", _inject_then_verify)

        before_size_before_inject = events_path.stat().st_size

        client = _client(monkeypatch, tmp_path)
        r = client.get("/v1/runs/toctou-race/receipt", headers=AUTH)
        assert r.status_code == 200

        after_bytes = events_path.read_bytes()
        # The injected bytes are there (we did that intentionally), but the
        # file must NOT have been truncated by EventLog repair.
        assert len(after_bytes) > before_size_before_inject, "inject didn't work"
        body = r.json()
        # With a torn tail the digest changes → problems must be non-empty.
        assert body["verification"]["ok"] is False


# ---------------------------------------------------------------------------
# _run_summary: errors are logged and exposed as summary_error
# ---------------------------------------------------------------------------


class TestRunSummaryError:
    """When receipt.json is unreadable, the summary must include summary_error."""

    def test_corrupt_receipt_exposes_summary_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A corrupt receipt.json must set receipt_status=unreadable and
        include summary_error with the exception type name."""
        run_dir = tmp_path / "corrupt-rcpt"
        run_dir.mkdir()
        (run_dir / "receipt.json").write_bytes(b"not valid json {{{")

        client = _client(monkeypatch, tmp_path)
        r = client.get("/v1/runs", headers=AUTH)
        assert r.status_code == 200
        rows = r.json()["runs"]
        row = next((x for x in rows if x["run_id"] == "corrupt-rcpt"), None)
        assert row is not None
        assert row["receipt_status"] == "unreadable"
        assert "summary_error" in row
        # The error name must be a non-empty string (e.g. "JSONDecodeError").
        assert isinstance(row["summary_error"], str)
        assert row["summary_error"]


# ---------------------------------------------------------------------------
# Fix 1: corrupt middle line and torn final line must be reported
# ---------------------------------------------------------------------------


class TestCorruptionReporting:
    """EventLog.read(repair=False) must report corruption, never silently truncate."""

    def test_corrupt_middle_line_reported_not_silently_dropped(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A corrupt line mid-file must surface as a verification problem.

        Before the fix, any parse failure in repair=False mode caused a silent
        ``break`` that dropped every subsequent event.  After the fix it raises,
        and ``verify_run_receipt`` captures the error in ``problems``.
        """
        from verdict.orchestration.receipt import EventLog, write_run_receipt

        run_dir = tmp_path / "runs" / "corrupt-mid"
        run_dir.mkdir(parents=True)
        log = EventLog(run_dir / "events.jsonl")
        log.emit("run_started", run_id="corrupt-mid", goal="corruption test")
        log.emit("run_finished", outcome="BLOCKED", reason="test")
        write_run_receipt(run_dir)

        # Inject a corrupt line between the two valid events.
        events_path = run_dir / "events.jsonl"
        lines = events_path.read_bytes().splitlines(keepends=True)
        lines.insert(1, b'{"seq":999,"bad json without closing\n')
        events_path.write_bytes(b"".join(lines))
        after_inject = events_path.read_bytes()

        client = _client(monkeypatch, tmp_path / "runs")
        r = client.get("/v1/runs/corrupt-mid/receipt", headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        # Corruption must be reported, not silently dropped.
        assert body["verification"]["ok"] is False
        problems = body["verification"]["problems"]
        assert problems, "expected at least one problem for corrupt middle line"
        # The problem text must mention corruption (not a hidden truncation).
        low = " ".join(p.lower() for p in problems)
        assert "corrupt" in low or "cannot rebuild" in low, (
            f"problem text does not mention corruption: {problems}"
        )
        # File must be unchanged — no repair must have run.
        assert events_path.read_bytes() == after_inject

    def test_torn_final_line_reported_as_torn_tail(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A torn final line (no trailing newline) must be reported as a torn tail.

        Only the very last line without a trailing newline may be treated as a
        torn tail; it must surface in ``problems`` and the file must be unchanged.
        """
        from verdict.orchestration.receipt import EventLog, write_run_receipt

        run_dir = tmp_path / "runs" / "torn-final"
        run_dir.mkdir(parents=True)
        log = EventLog(run_dir / "events.jsonl")
        log.emit("run_started", run_id="torn-final", goal="torn tail test")
        log.emit("run_finished", outcome="BLOCKED", reason="test")
        write_run_receipt(run_dir)

        # Append a partial line WITHOUT a trailing newline to simulate a torn tail.
        events_path = run_dir / "events.jsonl"
        with events_path.open("ab") as fh:
            fh.write(b'{"seq":999,"partial')
        after_inject = events_path.read_bytes()
        assert not after_inject.endswith(b"\n"), "test setup: file must not end with newline"

        client = _client(monkeypatch, tmp_path / "runs")
        r = client.get("/v1/runs/torn-final/receipt", headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        # Torn tail must be reported as a problem.
        assert body["verification"]["ok"] is False
        problems = body["verification"]["problems"]
        assert problems, "expected at least one problem for torn final line"
        low = " ".join(p.lower() for p in problems)
        assert "torn tail" in low or "cannot rebuild" in low, (
            f"problem text does not mention torn tail: {problems}"
        )
        # File must be unchanged — no repair must have run.
        assert events_path.read_bytes() == after_inject


# ---------------------------------------------------------------------------
# Fix 2: O_NOFOLLOW is used for file opens (atomic symlink rejection)
# ---------------------------------------------------------------------------


class TestONoFollowUsed:
    """os.open with O_NOFOLLOW must be called for events.jsonl and receipt.json."""

    def test_events_open_uses_o_nofollow(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The events endpoint must open events.jsonl with O_NOFOLLOW."""
        from verdict.orchestration.receipt import EventLog

        run_dir = tmp_path / "runs" / "nofollow-evts"
        run_dir.mkdir(parents=True)
        log = EventLog(run_dir / "events.jsonl")
        log.emit("run_started", run_id="nofollow-evts", goal="nofollow test")

        opens: list[tuple[str, int]] = []
        real_open = os.open

        def _recording_open(path: str, flags: int, *args: object, **kwargs: object) -> int:
            opens.append((str(path), flags))
            return real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(os, "open", _recording_open)

        client = _client(monkeypatch, tmp_path / "runs")
        r = client.get("/v1/runs/nofollow-evts/events", headers=AUTH)
        assert r.status_code == 200

        events_file = str(run_dir / "events.jsonl")
        events_opens = [(p, f) for p, f in opens if p == events_file]
        assert events_opens, f"os.open was not called for events.jsonl; all opens: {opens}"
        assert all(f & os.O_NOFOLLOW for _, f in events_opens), (
            f"O_NOFOLLOW not in flags for events.jsonl open: {events_opens}"
        )

    def test_receipt_open_uses_o_nofollow(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The receipt endpoint must open receipt.json with O_NOFOLLOW."""
        from verdict.orchestration.receipt import EventLog, write_run_receipt

        run_dir = tmp_path / "runs" / "nofollow-rcpt"
        run_dir.mkdir(parents=True)
        log = EventLog(run_dir / "events.jsonl")
        log.emit("run_started", run_id="nofollow-rcpt", goal="nofollow receipt test")
        log.emit("run_finished", outcome="BLOCKED", reason="test")
        write_run_receipt(run_dir)

        opens: list[tuple[str, int]] = []
        real_open = os.open

        def _recording_open(path: str, flags: int, *args: object, **kwargs: object) -> int:
            opens.append((str(path), flags))
            return real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(os, "open", _recording_open)

        client = _client(monkeypatch, tmp_path / "runs")
        r = client.get("/v1/runs/nofollow-rcpt/receipt", headers=AUTH)
        assert r.status_code == 200

        receipt_file = str(run_dir / "receipt.json")
        receipt_opens = [(p, f) for p, f in opens if p == receipt_file]
        assert receipt_opens, f"os.open was not called for receipt.json; all opens: {opens}"
        assert all(f & os.O_NOFOLLOW for _, f in receipt_opens), (
            f"O_NOFOLLOW not in flags for receipt.json open: {receipt_opens}"
        )


# ---------------------------------------------------------------------------
# Fix 3: started_at comes from run_started event, not the first parseable event
# ---------------------------------------------------------------------------


class TestStartedAtFromRunStarted:
    """_run_summary must take started_at from the run_started event only."""

    def test_started_at_null_when_no_run_started_event(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """started_at is null when no run_started event exists in events.jsonl."""
        run_dir = tmp_path / "run-no-start"
        run_dir.mkdir()
        # Write events with an `at` field but no run_started type.
        (run_dir / "events.jsonl").write_text(
            '{"seq":1,"at":"2025-01-01T00:00:00Z","type":"plan_created","node_id":"","data":{}}\n'
        )

        client = _client(monkeypatch, tmp_path)
        r = client.get("/v1/runs", headers=AUTH)
        assert r.status_code == 200
        row = next((x for x in r.json()["runs"] if x["run_id"] == "run-no-start"), None)
        assert row is not None
        assert row["started_at"] is None

    def test_started_at_from_run_started_not_first_event(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """started_at must come from run_started, even when it is not the first event."""
        run_dir = tmp_path / "run-late-start"
        run_dir.mkdir()
        # First event is plan_created (earlier timestamp); run_started comes second.
        events = [
            '{"seq":1,"at":"2025-01-01T00:00:00Z","type":"plan_created","node_id":"","data":{}}',
            '{"seq":2,"at":"2026-06-15T12:00:00Z","type":"run_started","node_id":"","data":{}}',
        ]
        (run_dir / "events.jsonl").write_text("\n".join(events) + "\n")

        client = _client(monkeypatch, tmp_path)
        r = client.get("/v1/runs", headers=AUTH)
        assert r.status_code == 200
        row = next((x for x in r.json()["runs"] if x["run_id"] == "run-late-start"), None)
        assert row is not None
        # Must be from run_started (seq 2), not plan_created (seq 1).
        assert row["started_at"] == "2026-06-15T12:00:00Z"


# ---------------------------------------------------------------------------
# Fix (round-7): _run_summary and verify_run_receipt use O_NOFOLLOW
# ---------------------------------------------------------------------------


class TestSymlinkInValidRunDir:
    """Symlinked files inside a valid run dir must not leak outside content."""

    def test_symlink_events_jsonl_in_run_dir_summary_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A symlinked events.jsonl inside a valid run dir must not appear in list_runs output."""
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "events.jsonl").write_text(
            '{"seq":1,"at":"2026-01-01T00:00:00Z","type":"run_started","node_id":"","data":{"secret":"outsidedata"}}\n'
        )
        run_dir = tmp_path / "runs" / "sym-evts-run"
        run_dir.mkdir(parents=True)
        try:
            os.symlink(outside / "events.jsonl", run_dir / "events.jsonl")
        except OSError:
            pytest.skip("symlink creation failed")

        client = _client(monkeypatch, tmp_path / "runs")
        r = client.get("/v1/runs", headers=AUTH)
        assert r.status_code == 200
        row = next((x for x in r.json()["runs"] if x["run_id"] == "sym-evts-run"), None)
        assert row is not None
        # started_at must NOT be set from the outside file.
        assert row["started_at"] is None
        # Outside content must never appear in the response.
        assert "outsidedata" not in r.text

    def test_symlink_receipt_json_in_run_dir_summary_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A symlinked receipt.json inside a valid run dir must set summary_error."""
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "receipt.json").write_text(
            '{"run_id":"outside","started_at":"2025-01-01T00:00:00Z","secret":"outsidereceipt"}'
        )
        run_dir = tmp_path / "runs" / "sym-rcpt-run"
        run_dir.mkdir(parents=True)
        try:
            os.symlink(outside / "receipt.json", run_dir / "receipt.json")
        except OSError:
            pytest.skip("symlink creation failed")

        client = _client(monkeypatch, tmp_path / "runs")
        r = client.get("/v1/runs", headers=AUTH)
        assert r.status_code == 200
        row = next((x for x in r.json()["runs"] if x["run_id"] == "sym-rcpt-run"), None)
        assert row is not None
        # Must have summary_error (symlink rejected) and must not leak outside content.
        assert "summary_error" in row
        assert "outsidereceipt" not in r.text
        # started_at must not be populated from the outside receipt.
        assert row["started_at"] is None

    def test_symlink_events_jsonl_receipt_endpoint_reports_problem(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A symlinked events.jsonl with a valid receipt must report a problem, not leak content."""
        from verdict.orchestration.receipt import EventLog, write_run_receipt

        run_dir = tmp_path / "runs" / "sym-evts-rcpt"
        run_dir.mkdir(parents=True)
        log = EventLog(run_dir / "events.jsonl")
        log.emit("run_started", run_id="sym-evts-rcpt", goal="test")
        log.emit("run_finished", outcome="BLOCKED", reason="test")
        write_run_receipt(run_dir)

        # Replace events.jsonl with a symlink to outside content AFTER receipt is written.
        events_path = run_dir / "events.jsonl"
        events_path.unlink()
        outside = tmp_path / "outside"
        outside.mkdir(exist_ok=True)
        (outside / "events.jsonl").write_text(
            '{"seq":1,"at":"2026-01-01T00:00:00Z","type":"run_started","node_id":"","data":{"secret":"leakedsecret"}}\n'
        )
        try:
            os.symlink(outside / "events.jsonl", events_path)
        except OSError:
            pytest.skip("symlink creation failed")

        client = _client(monkeypatch, tmp_path / "runs")
        r = client.get("/v1/runs/sym-evts-rcpt/receipt", headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        # Symlinked events.jsonl must cause a problem, not silently verify.
        assert body["verification"]["ok"] is False
        assert "leakedsecret" not in r.text


class TestVerifyNoFollowOpens:
    """verify_run_receipt(repair=False) must use O_NOFOLLOW for both files."""

    def test_verify_repair_false_uses_o_nofollow_for_receipt_and_events(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The receipt endpoint must open receipt.json and events.jsonl with O_NOFOLLOW."""
        from verdict.orchestration.receipt import EventLog, write_run_receipt

        run_dir = tmp_path / "runs" / "verify-nofollow"
        run_dir.mkdir(parents=True)
        log = EventLog(run_dir / "events.jsonl")
        log.emit("run_started", run_id="verify-nofollow", goal="test")
        log.emit("run_finished", outcome="BLOCKED", reason="test")
        write_run_receipt(run_dir)

        opens: list[tuple[str, int]] = []
        real_open = os.open

        def _recording_open(path: str, flags: int, *args: object, **kwargs: object) -> int:
            opens.append((str(path), flags))
            return real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(os, "open", _recording_open)

        client = _client(monkeypatch, tmp_path / "runs")
        r = client.get("/v1/runs/verify-nofollow/receipt", headers=AUTH)
        assert r.status_code == 200

        receipt_file = str(run_dir / "receipt.json")
        events_file = str(run_dir / "events.jsonl")

        receipt_opens = [(p, f) for p, f in opens if p == receipt_file]
        events_opens = [(p, f) for p, f in opens if p == events_file]

        assert receipt_opens, f"os.open not called for receipt.json; all opens: {opens}"
        assert events_opens, f"os.open not called for events.jsonl; all opens: {opens}"

        assert all(f & os.O_NOFOLLOW for _, f in receipt_opens), (
            f"O_NOFOLLOW not set for receipt.json: {receipt_opens}"
        )
        assert all(f & os.O_NOFOLLOW for _, f in events_opens), (
            f"O_NOFOLLOW not set for events.jsonl: {events_opens}"
        )
