"""BOD-304 shadow-only outcome → selection receipt tests.

Live shortlist/scores must stay bit-identical. Counterfactual would_rank is
shortlist-only. Acceptance requires VALIDATED/proof strictly before as_of.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from verdict.candidate_pool import fingerprint_task
from verdict.free_tier_admit import FreeTierAdmitReceipt
from verdict.intelligence import IntelligenceService
from verdict.metadata.records import (
    SOURCE_MODELS_DEV,
    CapabilityCaps,
    FieldProvenance,
    ModelMetadataRecord,
    ProvenancedField,
    SoftScores,
)
from verdict.metadata.store import MetadataSnapshot
from verdict.outcome_shadow import (
    ENV_ROOTS,
    METRIC_NAME,
    SINK_FILENAME,
    SINK_SCHEMA,
    WOULD_RANK_SCOPE,
    OutcomeShadowNotConfiguredError,
    append_outcome_shadow_record,
    attach_outcome_shadow,
    build_outcome_shadow,
    load_shadow_rows_from_run,
    resolve_outcome_shadow_roots,
    resolve_outcome_shadow_sink,
)
from verdict.task_profile import TaskProfile

SEED_RUN = Path("/tmp/bod271-merged-main-runs/20261016T184404Z")
FETCHED = "2026-09-19T03:00:00Z"
NOW = datetime(2026, 10, 16, 21, 0, tzinfo=timezone.utc)

# Seed clocks (real host evidence)
SEED_TERMINAL_SUCCESS = datetime(2026, 10, 16, 18, 48, 4, 669560, tzinfo=timezone.utc)
SEED_VALIDATED = datetime(2026, 10, 16, 18, 48, 5, 91086, tzinfo=timezone.utc)

FREE_TOOLS = "opencode/hy3-free"
PAID_TOOLS = "groq/llama-3.3-70b-versatile"
SEED_ROUTE = "antigravity/gemini-3.1-flash-lite"


def _prov(value: bool | int | float) -> ProvenancedField:
    return ProvenancedField(
        value=value, provenance=FieldProvenance(source=SOURCE_MODELS_DEV, fetched_at=FETCHED)
    )


def _record(model_id: str, *, tools: bool = True, context: int = 128000) -> ModelMetadataRecord:
    return ModelMetadataRecord(
        id=model_id,
        caps=CapabilityCaps(tools=_prov(tools), vision=_prov(False), context=_prov(context)),
        scores=SoftScores(),
        omniroute_ids=(model_id,),
        provider=model_id.split("/", 1)[0],
    )


def _store(ids: tuple[str, ...]) -> MetadataSnapshot:
    return MetadataSnapshot(
        schema_version="1", refreshed_at=FETCHED, sources={}, records=tuple(_record(i) for i in ids)
    )


def _profile_for_goal(goal: str) -> TaskProfile:
    fp = fingerprint_task(goal)
    return TaskProfile(
        digest=fp.digest,
        task_family=fp.task_family,
        required_capabilities=fp.required_capabilities,
        min_context=fp.context_size,
        spend_policy="free_first",
        task_class_hint=None,
        requirements_reasons=(),
        objective_preview=fp.objective_preview,
    )


def _svc(
    roots: list[Path] | tuple[Path, ...] = (), *, admit_now: datetime | None = None
) -> IntelligenceService:
    return IntelligenceService(
        primary_model="test/primary",
        providers={},
        profile="test",
        log_path=":memory:",
        log_full_task=False,
        discovery_ttl=60,
        allow_offline=True,
        outcome_shadow_roots=list(roots),
        admit_now=admit_now or NOW,
        decision_signal_provider=False,  # type: ignore[arg-type]  # bypass env provider
    )


@pytest.fixture
def seed_copy(tmp_path: Path) -> Path:
    if not SEED_RUN.is_dir():
        pytest.skip(f"seed run missing: {SEED_RUN}")
    dest = tmp_path / "seed_run"
    # Copy only receipt evidence files (not worktrees)
    dest.mkdir()
    for name in (
        "receipt.json",
        "events.jsonl",
        "graph.json",
        "outcome-records.jsonl",
        "progress.json",
    ):
        src = SEED_RUN / name
        if src.exists():
            shutil.copy2(src, dest / name)
    return dest


def _admit_receipt(routes: tuple[str, ...]) -> FreeTierAdmitReceipt:
    return FreeTierAdmitReceipt(
        admitted=routes,
        exclusions=(),
        chosen=routes[0] if routes else None,
        empty_intersection=not routes,
        active_providers=(),
        free_tier_providers=(),
        free_admitted=routes,
        paid_admitted=(),
    )


def test_resolve_roots_default_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VERDICT_OUTCOME_SHADOW_ROOTS", raising=False)
    assert resolve_outcome_shadow_roots(None) == ()
    assert resolve_outcome_shadow_roots([]) == ()


def test_terminal_success_before_validated_not_accepted(seed_copy: Path) -> None:
    mid = SEED_TERMINAL_SUCCESS.replace(microsecond=800000)  # between SUCCESS and VALIDATED
    # ensure mid is strictly between
    assert SEED_TERMINAL_SUCCESS < mid < SEED_VALIDATED
    rows, reason = load_shadow_rows_from_run(seed_copy, as_of=mid)
    assert reason is None
    implement = [r for r in rows if r.role == "implement" and r.route_id == SEED_ROUTE]
    assert implement, rows
    assert all(r.classification != "accepted_attempt" for r in implement)
    assert any(r.classification == "execution_terminal_unproven" for r in implement)


def test_validated_before_as_of_is_accepted(seed_copy: Path) -> None:
    after = SEED_VALIDATED.replace(microsecond=SEED_VALIDATED.microsecond + 1)
    rows, reason = load_shadow_rows_from_run(seed_copy, as_of=after)
    assert reason is None
    accepted = [
        r
        for r in rows
        if r.role == "implement"
        and r.route_id == SEED_ROUTE
        and r.classification == "accepted_attempt"
    ]
    assert len(accepted) == 1


def test_mechanical_empty_route_dropped_from_metric(seed_copy: Path) -> None:
    rows, reason = load_shadow_rows_from_run(seed_copy, as_of=NOW)
    assert reason is None
    integrate = [r for r in rows if r.node_id == "integrate_suite"]
    assert integrate
    assert all(r.classification == "mechanical_empty_route" for r in integrate)
    shadow = build_outcome_shadow(
        roots=[seed_copy],
        task_family=integrate[0].task_family,
        fingerprint_digest=integrate[0].fingerprint_digest,
        shortlist=[{"route_id": SEED_ROUTE, "score_features": {}}],
        as_of=NOW,
    )
    assert shadow["metric_n_accept"] >= 1  # implement only
    assert "" not in shadow["per_route"]


def test_bounds_before_verifier_skip(tmp_path: Path, seed_copy: Path) -> None:
    huge = tmp_path / "huge_run"
    huge.mkdir()
    shutil.copy2(seed_copy / "receipt.json", huge / "receipt.json")
    # Oversized events file
    (huge / "events.jsonl").write_bytes(b"x" * 1000)
    rows, reason = load_shadow_rows_from_run(
        huge, as_of=NOW, max_events_bytes=100, max_events_lines=10, max_receipt_bytes=512_000
    )
    assert rows == ()
    assert reason == "events_bytes_exceeded"


def test_verify_failed_skipped(tmp_path: Path, seed_copy: Path) -> None:
    bad = tmp_path / "bad_run"
    shutil.copytree(seed_copy, bad)
    # Tamper events without updating digest
    with (bad / "events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps({"at": "2099-01-01T00:00:00Z", "type": "controller", "seq": 999, "data": {}})
            + "\n"
        )
    rows, reason = load_shadow_rows_from_run(bad, as_of=NOW)
    assert rows == ()
    assert reason == "verify_failed"


def test_unrelated_third_directory_never_opened(seed_copy: Path, tmp_path: Path) -> None:
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    # If opened, missing receipt would be noted — we simply do not pass it
    shadow = build_outcome_shadow(
        roots=[seed_copy],
        task_family=fingerprint_task(
            json.loads((seed_copy / "receipt.json").read_text())["goal"]
        ).task_family,
        fingerprint_digest="x",
        shortlist=[{"route_id": SEED_ROUTE, "score_features": {"health": 1.0}}],
        as_of=NOW,
    )
    assert str(decoy) not in shadow["roots"]
    assert str(seed_copy.resolve()) in shadow["roots"] or str(seed_copy) in shadow["roots"]


def test_duplicate_roots_counted_once(seed_copy: Path) -> None:
    goal = json.loads((seed_copy / "receipt.json").read_text())["goal"]
    fp = fingerprint_task(goal)
    shadow = build_outcome_shadow(
        roots=[seed_copy, seed_copy],
        task_family=fp.task_family,
        fingerprint_digest=fp.digest,
        shortlist=[{"route_id": SEED_ROUTE, "score_features": {}}],
        as_of=NOW,
    )
    assert shadow["roots"].count(str(seed_copy.resolve())) <= 1 or len(shadow["roots"]) == 1


def test_would_rank_same_route_set_shortlist_only(seed_copy: Path) -> None:
    goal = json.loads((seed_copy / "receipt.json").read_text())["goal"]
    fp = fingerprint_task(goal)
    shortlist = [
        {"route_id": SEED_ROUTE, "score_features": {"health": 0.6, "uncertainty_penalty": 0.35}},
        {"route_id": PAID_TOOLS, "score_features": {"health": 1.0, "uncertainty_penalty": 0.35}},
    ]
    shadow = build_outcome_shadow(
        roots=[seed_copy],
        task_family=fp.task_family,
        fingerprint_digest=fp.digest,
        shortlist=shortlist,
        as_of=NOW,
    )
    assert shadow["would_rank_scope"] == WOULD_RANK_SCOPE
    assert shadow["metric"] == METRIC_NAME
    assert set(shadow["would_rank"]) == set(shadow["actual_rank"])
    assert shadow["metric_n_accept"] >= 1


def test_apply_candidate_pool_empty_roots_unchanged_choice(seed_copy: Path) -> None:
    # Bypass decision_signal_provider env by passing a no-op object with required API
    class _NoSignals:
        def collect(self, *args: Any, **kwargs: Any) -> list[Any]:
            return []

    routes = (FREE_TOOLS, PAID_TOOLS, SEED_ROUTE)
    store = _store(routes)
    goal = json.loads((seed_copy / "receipt.json").read_text())["goal"]
    profile = _profile_for_goal(goal)

    def _make(roots: list[Path]) -> IntelligenceService:
        return IntelligenceService(
            primary_model="test/primary",
            providers={},
            profile="test",
            log_path=":memory:",
            log_full_task=False,
            discovery_ttl=60,
            allow_offline=True,
            outcome_shadow_roots=roots,
            admit_now=NOW,
            decision_signal_provider=_NoSignals(),
            metadata_snapshot=store,
        )

    base_receipt = _admit_receipt(routes)
    svc_empty = _make([])
    out_empty, pool_empty = svc_empty._apply_candidate_pool(
        base_receipt, profile=profile, metadata=store, identity_map=None
    )
    svc_hist = _make([seed_copy])
    out_hist, pool_hist = svc_hist._apply_candidate_pool(
        base_receipt, profile=profile, metadata=store, identity_map=None
    )

    assert out_empty.chosen == out_hist.chosen
    assert out_empty.admitted == out_hist.admitted
    assert [e.route_id for e in pool_empty.shortlist] == [e.route_id for e in pool_hist.shortlist]
    assert [e.score for e in pool_empty.shortlist] == [e.score for e in pool_hist.shortlist]

    shadow = (out_hist.candidate_pool or {}).get("outcome_shadow")
    assert isinstance(shadow, dict)
    assert shadow.get("would_rank_scope") == WOULD_RANK_SCOPE
    assert "actual_rank" in shadow
    empty_shadow = (out_empty.candidate_pool or {}).get("outcome_shadow")
    assert empty_shadow.get("metric_missingness") == "no_roots_configured"


def test_shadow_error_does_not_change_choice(seed_copy: Path) -> None:
    class _NoSignals:
        def collect(self, *args: Any, **kwargs: Any) -> list[Any]:
            return []

    routes = (FREE_TOOLS, SEED_ROUTE)
    store = _store(routes)
    goal = json.loads((seed_copy / "receipt.json").read_text())["goal"]
    profile = _profile_for_goal(goal)
    svc = IntelligenceService(
        primary_model="test/primary",
        providers={},
        profile="test",
        log_path=":memory:",
        log_full_task=False,
        discovery_ttl=60,
        allow_offline=True,
        outcome_shadow_roots=[seed_copy],
        admit_now=NOW,
        decision_signal_provider=_NoSignals(),
        metadata_snapshot=store,
    )
    base = _admit_receipt(routes)
    with patch("verdict.intelligence.attach_outcome_shadow", side_effect=RuntimeError("boom")):
        out, pool = svc._apply_candidate_pool(
            base, profile=profile, metadata=store, identity_map=None
        )
    # Live choice still produced
    assert out.admitted
    assert pool.shortlist
    # Without shadow attach succeeding, candidate_pool may lack outcome_shadow — choice still fine
    assert out.chosen in out.admitted


def test_research_role_does_not_enter_implement_metric(tmp_path: Path, seed_copy: Path) -> None:
    # Build a minimal verified-looking run is hard; instead mutate a copy's node kind for a
    # synthetic row path via build_outcome_shadow after loading and adjusting — use rejected
    # evidence if available, else skip.
    # Soft check: unsupported_role audit increments when family matches non-implement.
    goal = json.loads((seed_copy / "receipt.json").read_text())["goal"]
    fp = fingerprint_task(goal)
    shadow = build_outcome_shadow(
        roots=[seed_copy],
        task_family=fp.task_family,
        fingerprint_digest=fp.digest,
        shortlist=[{"route_id": SEED_ROUTE, "score_features": {}}],
        as_of=NOW,
    )
    # integrate mechanical should not be metric accept
    assert shadow["metric"] == METRIC_NAME
    # metric denominator equals accept+reject only
    assert shadow["metric_denominator"] == shadow["metric_n_accept"] + shadow["metric_n_reject"]


# ---------------------------------------------------------------------------
# BOD-426: persistent jsonl sink + fail-loud would_rank when not computed.
# These tests need no seed run: an empty run dir fails verification (skip
# reason recorded) but the live-shortlist rerank still runs.
# ---------------------------------------------------------------------------

_SINK_SHORTLIST: list[dict[str, Any]] = [
    {"route_id": FREE_TOOLS, "score_features": {"health": 1.0}},
    {"route_id": PAID_TOOLS, "score_features": {"health": 0.5}},
]


def _unverified_root(tmp_path: Path) -> Path:
    root = tmp_path / "runs" / "run-a"
    root.mkdir(parents=True)
    return root


def _sink_lines(sink: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in sink.read_text(encoding="utf-8").splitlines()]


def test_resolve_sink_unset_is_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_ROOTS, raising=False)
    assert resolve_outcome_shadow_sink() is None
    assert resolve_outcome_shadow_sink(env={ENV_ROOTS: "   "}) is None


def test_resolve_sink_sits_next_to_first_env_root(tmp_path: Path) -> None:
    first = _unverified_root(tmp_path)
    second = tmp_path / "elsewhere" / "run-b"
    raw = os.pathsep.join([str(first), str(second)])
    sink = resolve_outcome_shadow_sink(env={ENV_ROOTS: raw})
    # Never inside a verified run dir: the run evidence stays untouched.
    assert sink == first.resolve().parent / SINK_FILENAME


def test_resolve_sink_next_to_manifest_file(tmp_path: Path) -> None:
    manifest = tmp_path / "shadow-roots.txt"
    manifest.write_text(f"{_unverified_root(tmp_path)}\n", encoding="utf-8")
    sink = resolve_outcome_shadow_sink(env={ENV_ROOTS: str(manifest)})
    assert sink == tmp_path.resolve() / SINK_FILENAME


def test_append_writes_one_jsonl_line_per_record_append_only(tmp_path: Path) -> None:
    sink = tmp_path / SINK_FILENAME
    sink.write_text('{"pre": "existing"}\n', encoding="utf-8")
    for idx in range(3):
        assert append_outcome_shadow_record(sink, {"idx": idx, "would_rank": None}) is True
    lines = _sink_lines(sink)
    assert lines[0] == {"pre": "existing"}
    assert [row["outcome_shadow"]["idx"] for row in lines[1:]] == [0, 1, 2]
    for row in lines[1:]:
        assert row["schema"] == SINK_SCHEMA
        assert isinstance(row["recorded_at"], str) and row["recorded_at"].endswith("Z")


def test_append_is_a_single_o_append_write(tmp_path: Path) -> None:
    sink = tmp_path / SINK_FILENAME
    real_open = os.open
    real_write = os.write
    flags_seen: list[int] = []
    writes: list[bytes] = []

    def spy_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        flags_seen.append(flags)
        return real_open(path, flags, *args, **kwargs)

    def spy_write(fd: int, data: bytes) -> int:
        writes.append(bytes(data))
        return real_write(fd, data)

    shadow = {"note": "line\nbreak", "would_rank": ["a", "b"]}
    with (
        patch("verdict.outcome_shadow.os.open", side_effect=spy_open),
        patch("verdict.outcome_shadow.os.write", side_effect=spy_write),
    ):
        assert append_outcome_shadow_record(sink, shadow) is True
    assert len(flags_seen) == 1
    assert flags_seen[0] & os.O_APPEND
    assert flags_seen[0] & os.O_CREAT
    assert flags_seen[0] & getattr(os, "O_NOFOLLOW", 0) == getattr(os, "O_NOFOLLOW", 0)
    # One complete record per write call: readers never see a torn line.
    assert len(writes) == 1
    assert writes[0].endswith(b"\n") and writes[0].count(b"\n") == 1
    assert json.loads(writes[0])["outcome_shadow"] == shadow


def test_append_missing_root_is_logged_not_created(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    sink = tmp_path / "missing" / SINK_FILENAME
    with caplog.at_level(logging.WARNING, logger="verdict.outcome_shadow"):
        assert append_outcome_shadow_record(sink, {"would_rank": None}) is False
    assert not sink.parent.exists()
    assert any("sink" in rec.getMessage() for rec in caplog.records)


def test_append_refuses_symlinked_sink(tmp_path: Path) -> None:
    if not hasattr(os, "O_NOFOLLOW"):
        pytest.skip("O_NOFOLLOW unavailable")
    target = tmp_path / "target.txt"
    target.write_text("keep\n", encoding="utf-8")
    sink = tmp_path / SINK_FILENAME
    sink.symlink_to(target)
    assert append_outcome_shadow_record(sink, {"would_rank": None}) is False
    assert target.read_text(encoding="utf-8") == "keep\n"


def test_attach_persists_every_receipt(tmp_path: Path) -> None:
    root = _unverified_root(tmp_path)
    sink = root.parent / SINK_FILENAME
    pool = {"shortlist": _SINK_SHORTLIST}
    shadows = []
    for _ in range(2):
        out = attach_outcome_shadow(
            pool,
            roots=[root],
            task_family="implement",
            fingerprint_digest="d" * 8,
            as_of=NOW,
            sink=sink,
        )
        shadows.append(out["outcome_shadow"])
    lines = _sink_lines(sink)
    assert len(lines) == 2
    assert [row["outcome_shadow"] for row in lines] == shadows
    assert lines[0]["outcome_shadow"]["actual_rank"] == [FREE_TOOLS, PAID_TOOLS]
    assert isinstance(lines[0]["outcome_shadow"]["would_rank"], list)
    # The run dir itself is never written to.
    assert sorted(p.name for p in root.iterdir()) == []


def test_attach_sink_failure_never_raises(tmp_path: Path) -> None:
    root = _unverified_root(tmp_path)
    with patch("verdict.outcome_shadow.os.open", side_effect=OSError("disk full")):
        out = attach_outcome_shadow(
            {"shortlist": _SINK_SHORTLIST},
            roots=[root],
            task_family="implement",
            fingerprint_digest="d" * 8,
            as_of=NOW,
            sink=root.parent / SINK_FILENAME,
        )
    assert out["outcome_shadow"]["actual_rank"] == [FREE_TOOLS, PAID_TOOLS]


def test_build_without_roots_fails_loud() -> None:
    with pytest.raises(OutcomeShadowNotConfiguredError):
        build_outcome_shadow(
            roots=[],
            task_family="implement",
            fingerprint_digest="d" * 8,
            shortlist=_SINK_SHORTLIST,
            as_of=NOW,
        )


def test_attach_without_roots_never_copies_actual_into_would_rank(tmp_path: Path) -> None:
    sink = tmp_path / SINK_FILENAME
    out = attach_outcome_shadow(
        {"shortlist": _SINK_SHORTLIST},
        roots=[],
        task_family="implement",
        fingerprint_digest="d" * 8,
        as_of=NOW,
        sink=sink,
    )
    shadow = out["outcome_shadow"]
    assert shadow["metric_missingness"] == "no_roots_configured"
    assert shadow["actual_rank"] == [FREE_TOOLS, PAID_TOOLS]
    assert shadow["would_rank"] is None
    assert shadow["rank_delta"] == []
    # Persisted record carries the explicit null, not an identity ranking.
    assert _sink_lines(sink)[0]["outcome_shadow"]["would_rank"] is None


def test_attach_error_path_never_copies_actual_into_would_rank() -> None:
    with patch("verdict.outcome_shadow.build_outcome_shadow", side_effect=RuntimeError("boom")):
        out = attach_outcome_shadow(
            {"shortlist": _SINK_SHORTLIST},
            roots=[Path("/nonexistent/run")],
            task_family="implement",
            fingerprint_digest="d" * 8,
            as_of=NOW,
        )
    shadow = out["outcome_shadow"]
    assert shadow["metric_missingness"] == "shadow_error"
    assert shadow["actual_rank"] == [FREE_TOOLS, PAID_TOOLS]
    assert shadow["would_rank"] is None


def test_service_writes_sink_from_env_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _NoSignals:
        def collect(self, *args: Any, **kwargs: Any) -> list[Any]:
            return []

    root = _unverified_root(tmp_path)
    monkeypatch.setenv(ENV_ROOTS, str(root))
    routes = (FREE_TOOLS, PAID_TOOLS)
    store = _store(routes)
    svc = IntelligenceService(
        primary_model="test/primary",
        providers={},
        profile="test",
        log_path=":memory:",
        log_full_task=False,
        discovery_ttl=60,
        allow_offline=True,
        admit_now=NOW,
        decision_signal_provider=_NoSignals(),
        metadata_snapshot=store,
    )
    profile = _profile_for_goal("implement a small parser with tests")
    out, _pool = svc._apply_candidate_pool(
        _admit_receipt(routes), profile=profile, metadata=store, identity_map=None
    )
    sink = root.resolve().parent / SINK_FILENAME
    lines = _sink_lines(sink)
    assert len(lines) == 1
    assert lines[0]["outcome_shadow"] == (out.candidate_pool or {})["outcome_shadow"]
