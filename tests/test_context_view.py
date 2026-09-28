"""
tests/test_context_view.py — BOD-278 lane 1: context provenance/budget projection tests.

Tests run against synthetic events only (no network, no repo I/O, no re-hydration).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from verdict.orchestration.context_view import _SCHEMA_VERSION, ContextView, context_view
from verdict.orchestration.contracts import RunEvent

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SEQ = 0


def _seq() -> int:
    global _SEQ
    _SEQ += 1
    return _SEQ


def _ev(type: str, node_id: str = "n1", **data: Any) -> RunEvent:
    return RunEvent(seq=_seq(), at="2026-09-28T14:00:00Z", type=type, node_id=node_id, data=data)


def _hydrate(
    node_id: str = "n1",
    budget_bytes: int = 60_000,
    prompt_bytes: int = 10_000,
    sources: list[dict[str, Any]] | None = None,
    truncated: bool = False,
) -> RunEvent:
    kw: dict[str, Any] = {
        "budget_bytes": budget_bytes,
        "prompt_bytes": prompt_bytes,
        "truncated": truncated,
        "context_files": [],
    }
    if sources is not None:
        kw["sources"] = sources
    return _ev("hydrate", node_id=node_id, **kw)


def _src(
    path: str = "README.md",
    size: int = 1000,
    included: bool = True,
    truncated_at: int | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    return {
        "path": path,
        "bytes": size,
        "included": included,
        "truncated_at": truncated_at,
        "reason": reason,
    }


# ---------------------------------------------------------------------------
# Budget pressure
# ---------------------------------------------------------------------------


class TestBudgetPressure:
    def test_under_budget(self) -> None:
        ev = _hydrate(budget_bytes=60_000, prompt_bytes=30_000)
        view = context_view([ev])
        node = view.nodes[0]
        assert node.budget_pressure == pytest.approx(0.5)

    def test_at_budget(self) -> None:
        ev = _hydrate(budget_bytes=60_000, prompt_bytes=60_000)
        view = context_view([ev])
        assert view.nodes[0].budget_pressure == pytest.approx(1.0)

    def test_over_budget(self) -> None:
        ev = _hydrate(budget_bytes=60_000, prompt_bytes=75_000)
        view = context_view([ev])
        assert view.nodes[0].budget_pressure == pytest.approx(1.25)

    def test_missing_budget_bytes_gives_none(self) -> None:
        ev = RunEvent(
            seq=_seq(),
            at="2026-09-28T14:00:00Z",
            type="hydrate",
            node_id="n2",
            data={"prompt_bytes": 5000, "context_files": []},
        )
        view = context_view([ev])
        assert view.nodes[0].budget_pressure is None

    def test_missing_prompt_bytes_gives_none(self) -> None:
        ev = RunEvent(
            seq=_seq(),
            at="2026-09-28T14:00:00Z",
            type="hydrate",
            node_id="n3",
            data={"budget_bytes": 60_000, "context_files": []},
        )
        view = context_view([ev])
        assert view.nodes[0].budget_pressure is None

    def test_zero_budget_gives_none(self) -> None:
        ev = RunEvent(
            seq=_seq(),
            at="2026-09-28T14:00:00Z",
            type="hydrate",
            node_id="n4",
            data={"budget_bytes": 0, "prompt_bytes": 5000, "context_files": []},
        )
        view = context_view([ev])
        assert view.nodes[0].budget_pressure is None


# ---------------------------------------------------------------------------
# Source states
# ---------------------------------------------------------------------------


class TestSourceStates:
    def test_included_source(self) -> None:
        src = _src("a.py", 500, included=True, truncated_at=None, reason=None)
        ev = _hydrate(sources=[src])
        view = context_view([ev])
        s = view.nodes[0].sources
        assert s is not None
        assert s[0].state == "included"
        assert s[0].bytes == 500
        assert s[0].reason is None
        assert s[0].truncated_at is None

    def test_truncated_source(self) -> None:
        src = _src("big.md", 10000, included=True, truncated_at=5000, reason="truncated")
        ev = _hydrate(sources=[src])
        view = context_view([ev])
        s = view.nodes[0].sources
        assert s is not None
        assert s[0].state == "truncated"
        assert s[0].truncated_at == 5000

    def test_excluded_budget_exhausted(self) -> None:
        src = _src("c.py", 1000, included=False, truncated_at=None, reason="budget_exhausted")
        ev = _hydrate(sources=[src])
        view = context_view([ev])
        s = view.nodes[0].sources
        assert s is not None
        assert s[0].state == "excluded"

    def test_excluded_unreadable(self) -> None:
        src = _src("missing.py", 0, included=False, truncated_at=None, reason="unreadable")
        ev = _hydrate(sources=[src])
        view = context_view([ev])
        s = view.nodes[0].sources
        assert s is not None
        assert s[0].state == "excluded"

    def test_deduplicated_state(self) -> None:
        entry = {
            "path": "dup.py",
            "bytes": 800,
            "included": False,
            "truncated_at": None,
            "reason": "deduplicated",
        }
        ev = _hydrate(sources=[entry])
        view = context_view([ev])
        assert view.nodes[0].sources is not None
        assert view.nodes[0].sources[0].state == "deduplicated"

    def test_compressed_state(self) -> None:
        entry = {
            "path": "compressed.py",
            "bytes": 2000,
            "included": False,
            "truncated_at": None,
            "reason": "compressed",
        }
        ev = _hydrate(sources=[entry])
        view = context_view([ev])
        assert view.nodes[0].sources is not None
        assert view.nodes[0].sources[0].state == "compressed"


# ---------------------------------------------------------------------------
# Totals by state
# ---------------------------------------------------------------------------


class TestTotalsByState:
    def test_totals_computed(self) -> None:
        srcs = [
            _src("a.py", 1000, included=True),
            _src("b.py", 2000, included=True),
            _src("c.py", 500, included=False, reason="budget_exhausted"),
        ]
        ev = _hydrate(sources=srcs)
        view = context_view([ev])
        totals = view.nodes[0].totals_by_state
        assert totals is not None
        assert totals.get("included") == 3000
        assert totals.get("excluded") == 500

    def test_totals_none_when_no_sources(self) -> None:
        ev = _hydrate()  # no sources key
        view = context_view([ev])
        assert view.nodes[0].totals_by_state is None


# ---------------------------------------------------------------------------
# Missing optional metrics -> None / unknown
# ---------------------------------------------------------------------------


class TestMissingMetrics:
    def test_missing_sources_field_gives_none(self) -> None:
        # Legacy event — no sources key in data
        ev = _hydrate(budget_bytes=60_000, prompt_bytes=5000)
        view = context_view([ev])
        node = view.nodes[0]
        assert node.sources is None
        assert node.totals_by_state is None

    def test_source_with_no_bytes_field(self) -> None:
        entry: dict[str, Any] = {
            "path": "x.py",
            "included": True,
            "truncated_at": None,
            "reason": None,
        }  # no "bytes" key
        ev = _hydrate(sources=[entry])
        view = context_view([ev])
        s = view.nodes[0].sources
        assert s is not None
        assert s[0].bytes is None  # unknown, not fabricated

    def test_source_unknown_state_when_no_included_flag(self) -> None:
        entry: dict[str, Any] = {
            "path": "y.py",
            "bytes": 300,
            "truncated_at": None,
            "reason": None,
        }  # no "included" key
        ev = _hydrate(sources=[entry])
        view = context_view([ev])
        s = view.nodes[0].sources
        assert s is not None
        assert s[0].state == "unknown"


# ---------------------------------------------------------------------------
# Legacy events without sources
# ---------------------------------------------------------------------------


class TestLegacyEvents:
    def test_legacy_event_no_crash(self) -> None:
        """Events from before #711 have no 'sources' field — view must say unknown, not empty."""
        ev = RunEvent(
            seq=_seq(),
            at="2026-09-28T14:09:15Z",
            type="hydrate",
            node_id="research",
            data={
                "budget_bytes": 60_000,
                "context_files": ["file1.md", "file2.md"],
                "prompt_bytes": 2379,
                "truncated": False,
            },
        )
        view = context_view([ev])
        node = view.nodes[0]
        assert node.node_id == "research"
        assert node.budget_bytes == 60_000
        assert node.sources is None  # unknown, NOT []
        assert node.totals_by_state is None  # unknown

    def test_legacy_event_budget_pressure_still_computable(self) -> None:
        ev = RunEvent(
            seq=_seq(),
            at="2026-09-28T14:09:15Z",
            type="hydrate",
            node_id="fix",
            data={
                "budget_bytes": 60_000,
                "prompt_bytes": 2463,
                "context_files": [],
                "truncated": False,
            },
        )
        view = context_view([ev])
        assert view.nodes[0].budget_pressure is not None

    def test_dogfood_run_dir(self, tmp_path: Path) -> None:
        """Replay the dogfood events.jsonl (pre-#711, no sources) — succeeds, sources=None."""
        import shutil

        dogfood = Path(__file__).parent.parent / "docs/proof/dogfood-bod-273-2026-09-28"
        if not dogfood.exists():
            pytest.skip("dogfood run dir not present")
        # copy to tmp so EventLog doesn't mutate it
        run_dir = tmp_path / "dogfood"
        shutil.copytree(dogfood, run_dir)
        view = context_view(run_dir)
        assert isinstance(view, ContextView)
        assert len(view.nodes) > 0
        for node in view.nodes:
            assert node.sources is None  # pre-#711 — no sources recorded

    def test_replay_deterministic(self, tmp_path: Path) -> None:
        """Building the view from the same events.jsonl gives identical JSON twice."""
        import shutil

        dogfood = Path(__file__).parent.parent / "docs/proof/dogfood-bod-273-2026-09-28"
        if not dogfood.exists():
            pytest.skip("dogfood run dir not present")
        run_dir = tmp_path / "dogfood2"
        shutil.copytree(dogfood, run_dir)
        v1 = context_view(run_dir)
        v2 = context_view(run_dir)
        assert v1.to_json() == v2.to_json()


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


class TestRedaction:
    def test_no_source_content_in_output(self) -> None:
        """Source content bytes must never appear — only metadata."""
        srcs = [_src("secret.py", 9999, included=True)]
        ev = _hydrate(sources=srcs)
        view = context_view([ev])
        out = view.to_json()
        # content string would be long; the path is fine to show
        assert "secret.py" in out
        # but no raw content chunks
        assert len(out) < 5000  # sanity: output is metadata only

    def test_api_key_in_reason_is_redacted(self) -> None:
        # redact_text catches key=value credential patterns (api_key=..., token=...)
        entry: dict[str, Any] = {
            "path": "cfg.py",
            "bytes": 100,
            "included": False,
            "truncated_at": None,
            "reason": "api_key=sk-abc123secretkey budget_exhausted",
        }
        ev = _hydrate(sources=[entry])
        view = context_view([ev])
        s = view.nodes[0].sources
        assert s is not None
        reason = s[0].reason or ""
        assert "sk-abc123secretkey" not in reason

    def test_to_dict_no_secrets(self) -> None:
        srcs = [_src("a.py", 500, included=True, reason="api_key=sk-abc123secretkey")]
        ev = _hydrate(sources=srcs)
        view = context_view([ev])
        d = view.to_dict()
        raw = json.dumps(d)
        # The serialised view must not carry a credential from a recorded reason.
        assert "sk-abc123secretkey" not in raw
        assert "schema_version" in d
        assert "nodes" in d


# ---------------------------------------------------------------------------
# Deterministic JSON (machine-readable)
# ---------------------------------------------------------------------------


class TestMachineReadable:
    def test_to_dict_stable_keys(self) -> None:
        srcs = [_src("a.py", 500), _src("b.py", 200, included=False, reason="budget_exhausted")]
        ev = _hydrate(sources=srcs)
        view = context_view([ev])
        d = view.to_dict()
        assert d["schema_version"] == _SCHEMA_VERSION
        assert isinstance(d["nodes"], list)
        node_d = d["nodes"][0]
        assert set(node_d.keys()) == {
            "node_id",
            "budget_bytes",
            "sources",
            "prompt_bytes",
            "totals_by_state",
            "budget_pressure",
        }

    def test_to_json_sorted_keys(self) -> None:
        srcs = [_src("a.py", 500)]
        ev = _hydrate(sources=srcs)
        view = context_view([ev])
        j1 = view.to_json()
        j2 = view.to_json()
        assert j1 == j2
        # Keys must be sorted (no random ordering)
        parsed = json.loads(j1)
        keys = list(parsed.keys())
        assert keys == sorted(keys)

    def test_to_json_valid_json(self) -> None:
        ev = _hydrate(sources=[_src("x.py", 100)])
        view = context_view([ev])
        parsed = json.loads(view.to_json())
        assert parsed["schema_version"] == _SCHEMA_VERSION

    def test_empty_events_gives_empty_nodes(self) -> None:
        view = context_view([])
        assert view.nodes == []
        d = view.to_dict()
        assert d["nodes"] == []

    def test_run_id_from_run_started_event(self) -> None:
        start = _ev("run_started", node_id="", run_id="test-run-42")
        h = _hydrate()
        view = context_view([start, h])
        assert view.run_id == "test-run-42"

    def test_run_id_none_when_no_run_started(self) -> None:
        view = context_view([_hydrate()])
        assert view.run_id is None


# ---------------------------------------------------------------------------
# Replay consistency / run_dir loading
# ---------------------------------------------------------------------------


class TestRunDirLoading:
    def _write_events(self, run_dir: Path, events: list[RunEvent]) -> None:
        (run_dir / "events.jsonl").parent.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps(e.to_dict(), sort_keys=True, separators=(",", ":")) for e in events]
        (run_dir / "events.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_run_dir_path_loads_events(self, tmp_path: Path) -> None:
        ev = _hydrate(node_id="loader-test", sources=[_src("f.py", 111)])
        self._write_events(tmp_path, [ev])
        view = context_view(tmp_path)
        assert len(view.nodes) == 1
        assert view.nodes[0].node_id == "loader-test"

    def test_run_dir_str_loads_events(self, tmp_path: Path) -> None:
        ev = _hydrate(node_id="str-test", sources=[_src("g.py", 222)])
        self._write_events(tmp_path, [ev])
        view = context_view(str(tmp_path))
        assert view.nodes[0].node_id == "str-test"

    def test_replay_same_result(self, tmp_path: Path) -> None:
        ev = _hydrate(sources=[_src("h.py", 333)])
        self._write_events(tmp_path, [ev])
        v1 = context_view(tmp_path)
        v2 = context_view(tmp_path)
        assert v1.to_json() == v2.to_json()

    def test_rehydrate_supersedes_hydrate(self) -> None:
        """Last hydrate/rehydrate per node_id wins."""
        h1 = _hydrate(
            node_id="n", budget_bytes=60_000, prompt_bytes=1000, sources=[_src("a.py", 100)]
        )
        h2 = RunEvent(
            seq=_seq(),
            at="2026-09-28T15:00:00Z",
            type="rehydrate",
            node_id="n",
            data={
                "budget_bytes": 30_000,
                "prompt_bytes": 2000,
                "context_files": [],
                "sources": [_src("a.py", 200)],
            },
        )
        view = context_view([h1, h2])
        assert len(view.nodes) == 1
        assert view.nodes[0].budget_bytes == 30_000
        assert view.nodes[0].prompt_bytes == 2000

    def test_multiple_nodes(self) -> None:
        evs = [
            _hydrate(
                node_id="n1", budget_bytes=60_000, prompt_bytes=5000, sources=[_src("a.py", 100)]
            ),
            _hydrate(
                node_id="n2", budget_bytes=40_000, prompt_bytes=20_000, sources=[_src("b.py", 500)]
            ),
        ]
        view = context_view(evs)
        assert len(view.nodes) == 2
        ids = {n.node_id for n in view.nodes}
        assert ids == {"n1", "n2"}
        assert view.node("n1") is not None
        assert view.node("n1").budget_bytes == 60_000
        assert view.node("missing") is None
