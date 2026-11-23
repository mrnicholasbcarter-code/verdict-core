from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import pytest

from tests.test_autodev_wiring import _route_verdict
from verdict.decomposer import (
    Decomposer,
    DecompositionConfig,
    DecompositionError,
    build_decomposition_prompt,
    parse_decomposition,
)

TWO_UNITS = [
    {
        "unit_id": "fix-cli-imports",
        "objective": "remove unused imports",
        "owned_files": ["verdict/cli.py"],
        "verification_command": ["ruff", "check", "--select", "F401", "verdict/cli.py"],
        "context": "",
    },
    {
        "unit_id": "fix-api-imports",
        "objective": "remove unused imports",
        "owned_files": ["verdict/api.py"],
        "verification_command": ["ruff", "check", "--select", "F401", "verdict/api.py"],
        "context": "",
    },
]


def _decomposer(content: str, *, status: int = 200, usage: dict[str, int] | None = None) -> Any:
    def transport(model_id: str, payload: Mapping[str, Any], timeout_seconds: float) -> Any:
        body: dict[str, Any] = {"choices": [{"message": {"content": content}}]}
        if usage is not None:
            body["usage"] = usage
        return {"status_code": status, "body": body}

    return Decomposer(DecompositionConfig(model="orch/model"), transport=transport)


def test_valid_plan_yields_multiple_units_with_measured_usage() -> None:
    decomposer = _decomposer(
        json.dumps(TWO_UNITS), usage={"prompt_tokens": 900, "completion_tokens": 240}
    )

    result = decomposer.decompose("fix ruff errors", repo_root=".")

    assert len(result.units) == 2
    assert [u.unit_id for u in result.units] == ["fix-cli-imports", "fix-api-imports"]
    assert result.units[0].verification_command[0] == "ruff"
    assert result.usage.reported is True
    assert result.usage.total_tokens == 1140


def test_unit_without_a_verification_command_is_a_decomposition_failure() -> None:
    broken = [dict(TWO_UNITS[0], verification_command=[])]

    with pytest.raises(DecompositionError, match="invalid work unit"):
        parse_decomposition(json.dumps(broken))


def test_unit_missing_the_verification_key_entirely_is_a_failure() -> None:
    broken = [{k: v for k, v in TWO_UNITS[0].items() if k != "verification_command"}]

    with pytest.raises(DecompositionError, match="missing field"):
        parse_decomposition(json.dumps(broken))


def test_units_may_not_claim_the_same_file() -> None:
    overlapping = [TWO_UNITS[0], dict(TWO_UNITS[1], owned_files=["verdict/cli.py"])]

    with pytest.raises(DecompositionError, match="disjoint files"):
        parse_decomposition(json.dumps(overlapping))


def test_escaping_owned_path_is_a_decomposition_failure() -> None:
    escaping = [dict(TWO_UNITS[0], owned_files=["../../etc/passwd"])]

    with pytest.raises(DecompositionError, match="invalid work unit"):
        parse_decomposition(json.dumps(escaping))


def test_prose_response_is_a_decomposition_failure() -> None:
    with pytest.raises(DecompositionError, match="not JSON"):
        parse_decomposition("I would split this into three parts.")


def test_empty_plan_is_a_decomposition_failure() -> None:
    with pytest.raises(DecompositionError, match="no work units"):
        parse_decomposition("[]")


def test_fenced_and_wrapped_responses_are_tolerated() -> None:
    fenced = f"```json\n{json.dumps(TWO_UNITS)}\n```"
    assert len(parse_decomposition(fenced)) == 2

    wrapped = json.dumps({"units": TWO_UNITS})
    assert len(parse_decomposition(wrapped)) == 2

    chatty = f"Here is the plan:\n{json.dumps(TWO_UNITS)}\nLet me know."
    assert len(parse_decomposition(chatty)) == 2


def test_unit_count_above_the_limit_is_rejected() -> None:
    many = [dict(TWO_UNITS[0], unit_id=f"u{i}", owned_files=[f"f{i}.py"]) for i in range(5)]

    with pytest.raises(DecompositionError, match="above the limit"):
        parse_decomposition(json.dumps(many), max_units=4)


def test_transport_and_http_failures_surface_as_decomposition_errors() -> None:
    def failing(model_id: str, payload: Mapping[str, Any], timeout_seconds: float) -> Any:
        raise ConnectionError("gateway down")

    with pytest.raises(DecompositionError, match="transport error"):
        Decomposer(DecompositionConfig(model="free/alpha:free"), transport=failing).decompose(
            "x", repo_root="."
        )

    with pytest.raises(DecompositionError, match="HTTP 500"):
        _decomposer(json.dumps(TWO_UNITS), status=500).decompose("x", repo_root=".")


def test_empty_objective_is_rejected_before_any_call() -> None:
    calls: list[str] = []

    def transport(model_id: str, payload: Mapping[str, Any], timeout_seconds: float) -> Any:
        calls.append(model_id)
        return {"status_code": 200, "body": {}}

    with pytest.raises(DecompositionError, match="non-empty string"):
        Decomposer(DecompositionConfig(model="free/alpha:free"), transport=transport).decompose(
            "  ", repo_root="."
        )
    assert calls == []


def test_prompt_carries_the_objective_and_evidence() -> None:
    prompt = build_decomposition_prompt("fix ruff errors", "/repo", "F401 verdict/cli.py:3")

    assert "fix ruff errors" in prompt
    assert "F401 verdict/cli.py:3" in prompt
    assert "/repo" in prompt


def test_code_plan_requires_owned_test_and_scoped_command() -> None:
    unit = dict(
        TWO_UNITS[0],
        objective="add validation",
        verification_command=["python", "-m", "pytest", "tests/test_cli.py"],
    )
    with pytest.raises(DecompositionError, match="test file"):
        parse_decomposition(json.dumps([unit]))


def test_invalid_code_plan_is_replanned_once() -> None:
    calls: list[str] = []
    unit = dict(
        TWO_UNITS[0],
        objective="add validation",
        verification_command=["python", "-m", "pytest", "tests/test_cli.py"],
    )
    valid = dict(unit, owned_files=["verdict/cli.py", "tests/test_cli.py"])

    def transport(model_id: str, payload: Mapping[str, Any], timeout_seconds: float) -> Any:
        calls.append(model_id)
        content = json.dumps([unit if len(calls) == 1 else valid])
        return {"status_code": 200, "body": {"choices": [{"message": {"content": content}}]}}

    config = DecompositionConfig(model="free/alpha:free")
    result = Decomposer(config, transport=transport).decompose("validation", repo_root=".")
    assert calls == ["free/alpha:free", "free/alpha:free"]
    assert result.units[0].owned_files == tuple(valid["owned_files"])
    assert result.to_dict()["attempts"][0]["error"]


# --- New tests for autodev-live-planner-selection (BOD-342 D22 lifted) ---


def test_cc_planner_is_no_longer_refused_by_decomposer() -> None:
    """After D22 lift, cc/* planner routes are no longer refused by Decomposer."""
    calls: list[str] = []

    def transport(model_id: str, payload: Mapping[str, Any], timeout_seconds: float) -> Any:
        calls.append(model_id)
        return {
            "status_code": 200,
            "body": {"choices": [{"message": {"content": json.dumps(TWO_UNITS)}}]},
        }

    # cc/* planner should now be accepted (no exclusion error)
    config = DecompositionConfig(model="cc/claude-opus-5")
    decomposer = Decomposer(config, transport=transport)
    result = decomposer.decompose("fix", repo_root=".")
    assert result.model == "cc/claude-opus-5"
    assert calls == ["cc/claude-opus-5"]


def test_decomposition_config_model_default_is_none() -> None:
    """DecompositionConfig.model should have no default (no hardcoded paid route)."""
    config = DecompositionConfig()
    assert config.model is None


def test_live_planner_selection_fails_closed_when_no_eligible_planners() -> None:
    """When no admissible route is in either planner band, autodev fails with a named error.

    With metadata=None both bands are empty, so a single-family ladder always
    hits the same named failure path (ADR-051).
    """
    import verdict.autodev_run as autodev
    from verdict.orchestration.contracts import CapacityClass

    # Only one family eligible; metadata=None → empty bands → named PlannerBandError
    verdicts = [_route_verdict("cc/claude-sonnet-5", rank=0, capacity=CapacityClass.SUBSCRIPTION)]

    class _Selector:
        def select(self, req: Any, *, now: Any) -> Any:
            return None, tuple(verdicts)

    with pytest.raises(autodev.AutodevError) as excinfo:
        autodev._live_planner_models(_Selector(), top_n=3)

    message = str(excinfo.value)
    assert "no_frontier_or_fallback_planner" in message


def _two_family_snapshot() -> Any:
    """Minimal MetadataSnapshot with free/alpha and cc/claude-sonnet-5 tied at 90.0.

    Both routes receive the same aa_intelligence score so they tie for the
    frontier-band cutoff (ceil(2*0.20)=1 → both included via tie rule).
    The FREE route sorts ahead of SUBSCRIPTION at equal score so it is
    ``planners[0]``, preserving the original test assertion.
    """
    from datetime import timezone

    from verdict.metadata.records import FieldProvenance, ModelMetadataRecord, ProvenancedField, SoftScores
    from verdict.metadata.store import MetadataSnapshot

    fetched = "2026-11-23T09:00:00Z"

    def _score(v: float) -> ProvenancedField:
        return ProvenancedField(
            value=v,
            provenance=FieldProvenance(source="openrouter.artificial_analysis", fetched_at=fetched),
        )

    return MetadataSnapshot(
        schema_version="1",
        refreshed_at=fetched,
        sources={},
        records=(
            ModelMetadataRecord(
                id="free/alpha",
                scores=SoftScores(aa_intelligence=_score(90.0)),
                omniroute_ids=("free/alpha",),
                provider="free",
            ),
            ModelMetadataRecord(
                id="cc/claude-sonnet-5",
                scores=SoftScores(aa_intelligence=_score(90.0)),
                omniroute_ids=("cc/claude-sonnet-5",),
                provider="cc",
            ),
        ),
    )


def test_live_planner_selection_called_for_dry_run_without_decision(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A dry run with no decision plans on the live ladder's two-family planner set.

    Catches the planner walk being cut to one route: ``_live_planner_models``
    needs two families, so a one-route walk fails every dry run closed.
    """
    import verdict.autodev_run as autodev
    import verdict.cli as cli
    from verdict.orchestration.contracts import CapacityClass

    verdicts = (
        _route_verdict("free/alpha:free", rank=0),
        _route_verdict("cc/claude-sonnet-5", rank=1, capacity=CapacityClass.SUBSCRIPTION),
    )

    class _Selector:
        def select(self, req: Any, *, now: Any) -> Any:
            return None, verdicts

    monkeypatch.setattr(
        "verdict.orchestration.eligibility_report.build_selector",
        lambda gateway, **kwargs: _Selector(),
    )
    # Provide metadata so both routes tie for the frontier band (ADR-051).
    _snap = _two_family_snapshot()
    monkeypatch.setattr("verdict.metadata.store.load_store", lambda *a, **kw: _snap)
    configs: list[Any] = []

    def transport(model_id: str, payload: Mapping[str, Any], timeout_seconds: float) -> Any:
        return {
            "status_code": 200,
            "body": {"choices": [{"message": {"content": json.dumps(TWO_UNITS)}}]},
        }

    def decomposer_factory(config: Any) -> Decomposer:
        configs.append(config)
        return Decomposer(config, transport=transport)

    monkeypatch.setattr("verdict.decomposer.Decomposer", decomposer_factory)

    def forbid_execution(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("dry-run must not authorize or execute a worker")

    monkeypatch.setattr(autodev, "_require_launch_decision", forbid_execution)
    monkeypatch.setattr(autodev, "run_autodev", forbid_execution)

    cli.cmd_autodev(
        "fix ruff errors",
        str(tmp_path),
        orchestrator_model=None,
        executor_model=None,
        base_url="http://127.0.0.1:20128/v1",
        dry_run=True,
        output_json=True,
    )
    capsys.readouterr()

    assert configs[0].model == "free/alpha:free"
    assert configs[0].planner_models == ("free/alpha:free", "cc/claude-sonnet-5")


def test_code_and_test_split_into_two_units_is_merged_before_rejecting() -> None:
    """The common codestral failure: code and its test come back as separate units."""
    split = [
        dict(
            TWO_UNITS[0],
            unit_id="implement-feature",
            objective="implement the feature",
            owned_files=["verdict/feature.py"],
            verification_command=["python", "-m", "pytest", "tests/test_feature.py"],
        ),
        dict(
            TWO_UNITS[0],
            unit_id="test-feature",
            objective="test the feature",
            owned_files=["tests/test_feature.py"],
            verification_command=["python", "-m", "pytest", "tests/test_feature.py"],
        ),
    ]

    units = parse_decomposition(json.dumps(split))

    assert len(units) == 1
    assert set(units[0].owned_files) == {"verdict/feature.py", "tests/test_feature.py"}


def test_replan_evidence_includes_a_one_unit_json_example() -> None:
    unit = dict(
        TWO_UNITS[0],
        objective="add validation",
        verification_command=["python", "-m", "pytest", "tests/test_cli.py"],
    )
    valid = dict(unit, owned_files=["verdict/cli.py", "tests/test_cli.py"])
    prompts: list[str] = []

    def transport(model_id: str, payload: Mapping[str, Any], timeout_seconds: float) -> Any:
        prompts.append(payload["messages"][1]["content"])
        content = json.dumps([unit if len(prompts) == 1 else valid])
        return {"status_code": 200, "body": {"choices": [{"message": {"content": content}}]}}

    config = DecompositionConfig(model="free/alpha:free")
    Decomposer(config, transport=transport).decompose("validation", repo_root=".")
    assert len(prompts) == 2
    assert '"owned_files"' in prompts[1]
    assert "tests/test_" in prompts[1]
    assert "one unit owning both" in prompts[1].lower()


def test_planner_moves_down_planner_models_after_two_rejections() -> None:
    calls: list[str] = []
    code_only = dict(
        TWO_UNITS[0],
        objective="add validation",
        verification_command=["python", "-m", "pytest", "tests/test_cli.py"],
    )

    def transport(model_id: str, payload: Mapping[str, Any], timeout_seconds: float) -> Any:
        calls.append(model_id)
        content = json.dumps([code_only] if len(calls) < 3 else TWO_UNITS)
        return {"status_code": 200, "body": {"choices": [{"message": {"content": content}}]}}

    config = DecompositionConfig(
        model="free/alpha:free", planner_models=("free/alpha:free", "agy/gemini-x")
    )
    result = Decomposer(config, transport=transport).decompose("fix", repo_root=".")
    assert calls == ["free/alpha:free", "free/alpha:free", "agy/gemini-x"]
    assert result.model == "agy/gemini-x"
    assert len(result.attempts) == 3


def test_transient_503_moves_to_the_next_planner_family() -> None:
    calls: list[str] = []

    def transport(model_id: str, payload: Mapping[str, Any], timeout_seconds: float) -> Any:
        calls.append(model_id)
        if model_id == "free/alpha:free":
            return {"status_code": 503, "body": {}}
        return {
            "status_code": 200,
            "body": {"choices": [{"message": {"content": json.dumps(TWO_UNITS)}}]},
        }

    config = DecompositionConfig(
        model="free/alpha:free", planner_models=("free/alpha:free", "agy/gemini-x")
    )
    result = Decomposer(config, transport=transport).decompose("fix", repo_root=".")
    # A transient 503 is never re-sent to the same model; the plan comes from the next family.
    assert calls == ["free/alpha:free", "agy/gemini-x"]
    assert result.model == "agy/gemini-x"
    assert [a["model"] for a in result.attempts] == ["free/alpha:free", "agy/gemini-x"]
    assert "HTTP 503" in result.attempts[0]["error"]
    assert len(result.units) == 2


def test_failure_carries_every_attempt_and_never_tries_cc() -> None:
    calls: list[str] = []
    split = [
        dict(TWO_UNITS[0], unit_id="one", owned_files=["verdict/r.py"]),
        dict(TWO_UNITS[1], unit_id="two", owned_files=["verdict/r.py"]),
    ]

    def transport(model_id: str, payload: Mapping[str, Any], timeout_seconds: float) -> Any:
        calls.append(model_id)
        if model_id == "agy/gemini-x":
            return {"status_code": 429, "body": {}}
        return {
            "status_code": 200,
            "body": {"choices": [{"message": {"content": json.dumps(split)}}]},
        }

    config = DecompositionConfig(
        model="free/alpha:free", planner_models=("free/alpha:free", "agy/gemini-x")
    )
    with pytest.raises(DecompositionError) as excinfo:
        Decomposer(config, transport=transport).decompose("fix", repo_root=".")
    assert not any(model.startswith("cc/") for model in calls)
    attempts = excinfo.value.attempts
    assert [a["model"] for a in attempts] == ["free/alpha:free", "free/alpha:free", "agy/gemini-x"]
    assert "both claim" in attempts[0]["error"]
    assert "HTTP 429" in attempts[2]["error"]
    # The raised message names every attempt, not only the last one.
    message = str(excinfo.value)
    assert "both claim" in message and "HTTP 429" in message and "free/alpha:free" in message


def test_units_editing_one_file_with_one_check_are_merged() -> None:
    """Live codestral shape (BOD-393): one rename per line, all owning the same file."""
    check = ["pytest", "tests/test_credentials_drift.py", "-q"]
    owned = ["verdict/credentials_registry.py", "tests/test_credentials_drift.py"]
    plan = [
        {
            "unit_id": f"rename-{n}",
            "objective": f"rename {n}",
            "owned_files": owned,
            "verification_command": check,
            "context": "",
        }
        for n in ("server", "dashboard", "all")
    ] + [
        {
            "unit_id": "update-fixture",
            "objective": "update the fixture",
            "owned_files": ["tests/fixtures/f.json", "tests/test_actions_dispatch_b.py"],
            "verification_command": ["pytest", "tests/test_actions_dispatch_b.py", "-q"],
            "context": "",
        }
    ]

    units = parse_decomposition(json.dumps(plan))

    assert [u.unit_id for u in units] == ["rename-server", "update-fixture"]
    assert units[0].owned_files == tuple(owned)
    assert "rename dashboard" in units[0].objective and "rename all" in units[0].objective


def test_transient_503_on_the_only_planner_raises_after_one_attempt() -> None:
    calls: list[str] = []

    def transport(model_id: str, payload: Mapping[str, Any], timeout_seconds: float) -> Any:
        calls.append(model_id)
        return {"status_code": 503, "body": {}}

    config = DecompositionConfig(model="free/alpha:free")
    with pytest.raises(DecompositionError, match="HTTP 503") as excinfo:
        Decomposer(config, transport=transport).decompose("fix", repo_root=".")
    # A transient error is not re-sent to the same model and there is no hidden fallback.
    assert calls == ["free/alpha:free"]
    assert len(excinfo.value.attempts) == 1
