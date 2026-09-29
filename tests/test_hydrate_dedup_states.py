"""tests/test_hydrate_dedup_states.py — BOD-278 AC3/AC9

Tests for:
- Path deduplication in _hydrate_sources (same file via "./" prefix and literal dup)
- Path deduplication in hydrate_node_prompt (prompt bytes/evidence agree)
- compression="not_performed" recorded in hydrate event and surfaced in NodeContextView
- Runtime → event → context_view() round-trip shows 'deduplicated' + compression
- ../  traversal left untouched (no wider access)
- Legacy events without the new fields still render without error
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from verdict.orchestration.context_view import context_view
from verdict.orchestration.contracts import RunEvent
from verdict.orchestration.planner import hydrate_node_prompt
from verdict.orchestration.runtime import _hydrate_sources, _normalize_context_path

# ---------------------------------------------------------------------------
# Helpers shared with test_context_view.py (duplicated here to keep tests
# independent — no shared fixture module)
# ---------------------------------------------------------------------------

_SEQ = 1000  # different offset to avoid any global-state collision


def _seq() -> int:
    global _SEQ
    _SEQ += 1
    return _SEQ


def _ev(type: str, node_id: str = "n1", **data: Any) -> RunEvent:
    return RunEvent(seq=_seq(), at="2026-09-29T10:00:00Z", type=type, node_id=node_id, data=data)


def _hydrate_ev(
    node_id: str = "n1",
    budget_bytes: int = 60_000,
    prompt_bytes: int = 10_000,
    sources: list[dict[str, Any]] | None = None,
    compression: str | None = None,
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
    if compression is not None:
        kw["compression"] = compression
    return _ev("hydrate", node_id=node_id, **kw)


# ---------------------------------------------------------------------------
# 1. _normalize_context_path
# ---------------------------------------------------------------------------


class TestNormalizeContextPath:
    def test_plain_path_unchanged(self) -> None:
        assert _normalize_context_path("a.py") == "a.py"

    def test_dotslash_stripped(self) -> None:
        assert _normalize_context_path("./a.py") == "a.py"

    def test_nested_dotslash_stripped(self) -> None:
        assert _normalize_context_path("./src/foo.py") == "src/foo.py"

    def test_double_dotslash_not_traversal(self) -> None:
        # "../" traversal is left as-is — upstream handles/rejects it
        assert _normalize_context_path("../outside.py") == "../outside.py"

    def test_dotdot_unchanged(self) -> None:
        assert _normalize_context_path("../secret.py") == "../secret.py"


# ---------------------------------------------------------------------------
# 2. _hydrate_sources deduplication
# ---------------------------------------------------------------------------


class TestHydrateSourcesDedup:
    def test_exact_duplicate_deduplicated(self, tmp_path: Path) -> None:
        f = tmp_path / "a.py"
        f.write_bytes(b"x" * 100)
        sources = _hydrate_sources(["a.py", "a.py"], tmp_path, 60_000)
        assert len(sources) == 2
        assert sources[0]["included"] is True
        assert sources[0]["reason"] is None
        assert sources[1]["included"] is False
        assert sources[1]["reason"] == "deduplicated"
        assert sources[1]["duplicate_of"] == "a.py"

    def test_dotslash_dedup(self, tmp_path: Path) -> None:
        """./a.py and a.py should be treated as the same file."""
        f = tmp_path / "a.py"
        f.write_bytes(b"y" * 200)
        sources = _hydrate_sources(["a.py", "./a.py"], tmp_path, 60_000)
        assert len(sources) == 2
        assert sources[0]["included"] is True
        assert sources[1]["included"] is False
        assert sources[1]["reason"] == "deduplicated"
        assert sources[1]["duplicate_of"] == "a.py"

    def test_dotslash_first_dedup(self, tmp_path: Path) -> None:
        """./a.py first, then a.py — first occurrence wins."""
        f = tmp_path / "a.py"
        f.write_bytes(b"z" * 50)
        sources = _hydrate_sources(["./a.py", "a.py"], tmp_path, 60_000)
        assert sources[0]["path"] == "./a.py"
        assert sources[0]["included"] is True
        assert sources[1]["included"] is False
        assert sources[1]["reason"] == "deduplicated"

    def test_three_copies_only_first_included(self, tmp_path: Path) -> None:
        f = tmp_path / "x.py"
        f.write_bytes(b"a" * 300)
        sources = _hydrate_sources(["x.py", "x.py", "./x.py"], tmp_path, 60_000)
        assert sources[0]["included"] is True
        assert sources[1]["included"] is False
        assert sources[1]["reason"] == "deduplicated"
        assert sources[2]["included"] is False
        assert sources[2]["reason"] == "deduplicated"

    def test_traversal_path_not_altered(self, tmp_path: Path) -> None:
        """../  path is NOT merged with anything — its canonical form stays ../..."""
        f1 = tmp_path / "a.py"
        f1.write_bytes(b"b" * 50)
        # ../a.py points outside worktree — OSError expected, reason="unreadable"
        sources = _hydrate_sources(["a.py", "../a.py"], tmp_path, 60_000)
        assert sources[0]["included"] is True
        assert sources[0]["path"] == "a.py"
        # ../a.py resolves outside tmp_path — should be unreadable (or deduplicated?)
        # The canonical form of "../a.py" is "../a.py" (not "a.py"), so NOT deduped
        assert sources[1]["path"] == "../a.py"
        assert sources[1]["reason"] != "deduplicated"  # must remain unreadable, not deduped

    def test_deduplicated_bytes_is_none(self, tmp_path: Path) -> None:
        """Deduplicated entries must carry bytes=None (we don't double-count bytes)."""
        f = tmp_path / "b.py"
        f.write_bytes(b"c" * 100)
        sources = _hydrate_sources(["b.py", "b.py"], tmp_path, 60_000)
        assert sources[1]["bytes"] is None


# ---------------------------------------------------------------------------
# 3. compression="not_performed" in event data and NodeContextView
# ---------------------------------------------------------------------------


class TestCompressionField:
    def test_compression_not_performed_in_nodeview(self) -> None:
        ev = _hydrate_ev(compression="not_performed")
        view = context_view([ev])
        assert view.nodes[0].compression == "not_performed"

    def test_compression_absent_in_legacy_event(self) -> None:
        """Legacy events without compression field give compression=None."""
        ev = _hydrate_ev()  # no compression kwarg
        view = context_view([ev])
        assert view.nodes[0].compression is None

    def test_compression_in_to_dict(self) -> None:
        ev = _hydrate_ev(compression="not_performed")
        view = context_view([ev])
        d = view.nodes[0].to_dict()
        assert "compression" in d
        assert d["compression"] == "not_performed"

    def test_compression_none_in_to_dict_for_legacy(self) -> None:
        ev = _hydrate_ev()
        view = context_view([ev])
        d = view.nodes[0].to_dict()
        assert "compression" in d
        assert d["compression"] is None

    def test_custom_compression_algorithm_passthrough(self) -> None:
        ev = _hydrate_ev(compression="zstd-v1")
        view = context_view([ev])
        assert view.nodes[0].compression == "zstd-v1"


# ---------------------------------------------------------------------------
# 4. Runtime → event → context_view() round-trip (BOD-278 AC9)
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_dedup_roundtrip_via_event(self, tmp_path: Path) -> None:
        """Simulate what the runtime emits and verify the view sees 'deduplicated'."""
        f = tmp_path / "foo.py"
        f.write_bytes(b"pass\n" * 50)
        raw_sources = _hydrate_sources(["foo.py", "foo.py", "./foo.py"], tmp_path, 60_000)
        ev = _hydrate_ev(
            sources=raw_sources, compression="not_performed", prompt_bytes=300, budget_bytes=60_000
        )
        view = context_view([ev])
        node = view.nodes[0]
        assert node.compression == "not_performed"
        assert node.sources is not None
        assert len(node.sources) == 3
        states = [s.state for s in node.sources]
        assert states[0] == "included"
        assert states[1] == "deduplicated"
        assert states[2] == "deduplicated"

    def test_dedup_roundtrip_reasons(self, tmp_path: Path) -> None:
        f = tmp_path / "bar.py"
        f.write_bytes(b"x" * 200)
        raw_sources = _hydrate_sources(["bar.py", "./bar.py"], tmp_path, 60_000)
        ev = _hydrate_ev(sources=raw_sources, compression="not_performed")
        view = context_view([ev])
        duped = [s for s in view.nodes[0].sources or [] if s.state == "deduplicated"]
        assert len(duped) == 1
        assert duped[0].reason == "deduplicated"

    def test_compression_not_performed_roundtrip(self, tmp_path: Path) -> None:
        f = tmp_path / "c.py"
        f.write_bytes(b"z" * 100)
        raw_sources = _hydrate_sources(["c.py"], tmp_path, 60_000)
        ev = _hydrate_ev(sources=raw_sources, compression="not_performed")
        # Serialize to jsonl and deserialize (replay)
        ev_dict = {"seq": ev.seq, "at": ev.at, "type": ev.type, "node_id": ev.node_id, **ev.data}
        line = json.dumps(ev_dict, sort_keys=True)
        ev2_data = json.loads(line)
        ev2 = RunEvent(
            seq=ev2_data["seq"],
            at=ev2_data["at"],
            type=ev2_data["type"],
            node_id=ev2_data["node_id"],
            data={k: v for k, v in ev2_data.items() if k not in ("seq", "at", "type", "node_id")},
        )
        view = context_view([ev2])
        assert view.nodes[0].compression == "not_performed"

    def test_prompt_and_evidence_agree_on_dedup(self, tmp_path: Path) -> None:
        """Prompt skips duplicate; evidence also marks it deduplicated.
        The set of paths in evidence that are NOT deduplicated matches
        the set of paths that appear in the prompt content.
        """
        fa = tmp_path / "a.py"
        fa.write_bytes(b"a = 1\n" * 10)
        fb = tmp_path / "b.py"
        fb.write_bytes(b"b = 2\n" * 10)

        from verdict.orchestration.contracts import WorkNode

        node = WorkNode(
            node_id="test",
            objective="test obj",
            owned_files=["a.py"],
            required_context=["a.py", "./a.py", "b.py"],
            acceptance=[],
            verification_command=["true"],
        )
        prompt = hydrate_node_prompt(node, repo=tmp_path, goal="G", max_context_bytes=60_000)
        evidence = _hydrate_sources(["a.py", "./a.py", "b.py"], tmp_path, 60_000)

        # Evidence: a.py included, ./a.py deduplicated, b.py included
        assert evidence[0]["included"] is True
        assert evidence[1]["included"] is False
        assert evidence[1]["reason"] == "deduplicated"
        assert evidence[2]["included"] is True

        # Prompt: a.py content appears exactly once, b.py appears once
        # (deduplicated paths produce a note in TRUNCATION_NOTES, not a content block)
        assert prompt.count("--- a.py ---") == 1
        assert prompt.count("--- b.py ---") == 1
        # The deduplicated entry leaves a note (not a content block)
        assert "deduplicated" in prompt

    def test_legacy_event_no_compression_still_renders(self) -> None:
        """Old events without compression field must not crash."""
        from verdict.orchestration.context_render import render_context_text

        ev = _hydrate_ev(
            sources=[
                {
                    "path": "f.py",
                    "bytes": 100,
                    "included": True,
                    "truncated_at": None,
                    "reason": None,
                }
            ]
        )
        view = context_view([ev])
        text = render_context_text(view, width=100)
        assert "f.py" in text
        # No "compression:" line for legacy events
        assert "compression:" not in text
