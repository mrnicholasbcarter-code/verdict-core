from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import pytest

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
        Decomposer(DecompositionConfig(), transport=failing).decompose("x", repo_root=".")

    with pytest.raises(DecompositionError, match="HTTP 500"):
        _decomposer(json.dumps(TWO_UNITS), status=500).decompose("x", repo_root=".")


def test_empty_objective_is_rejected_before_any_call() -> None:
    calls: list[str] = []

    def transport(model_id: str, payload: Mapping[str, Any], timeout_seconds: float) -> Any:
        calls.append(model_id)
        return {"status_code": 200, "body": {}}

    with pytest.raises(DecompositionError, match="non-empty string"):
        Decomposer(DecompositionConfig(), transport=transport).decompose("  ", repo_root=".")
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

    result = Decomposer(transport=transport).decompose("validation", repo_root=".")
    assert calls == ["mistral/codestral-latest", "mistral/codestral-latest"]
    assert result.units[0].owned_files == tuple(valid["owned_files"])
    assert result.to_dict()["attempts"][0]["error"]


def test_frontier_planner_only_after_recorded_cheap_failure() -> None:
    calls: list[str] = []

    def transport(model_id: str, payload: Mapping[str, Any], timeout_seconds: float) -> Any:
        calls.append(model_id)
        if len(calls) == 1:
            return {"status_code": 503, "body": {}}
        return {
            "status_code": 200,
            "body": {"choices": [{"message": {"content": json.dumps(TWO_UNITS)}}]},
        }

    result = Decomposer(
        DecompositionConfig(model="cc/claude-opus-5"), transport=transport
    ).decompose("fix", repo_root=".")
    assert calls == ["mistral/codestral-latest", "cc/claude-opus-5"]
    assert "HTTP 503" in result.attempts[0]["error"]


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

    Decomposer(transport=transport).decompose("validation", repo_root=".")
    assert len(prompts) == 2
    assert '"owned_files"' in prompts[1]
    assert "tests/test_" in prompts[1]
    assert "one unit owning both" in prompts[1].lower()


def test_planner_climbs_the_ladder_after_two_cheap_failures() -> None:
    calls: list[str] = []

    def transport(model_id: str, payload: Mapping[str, Any], timeout_seconds: float) -> Any:
        calls.append(model_id)
        if len(calls) < 3:
            return {"status_code": 503, "body": {}}
        return {
            "status_code": 200,
            "body": {"choices": [{"message": {"content": json.dumps(TWO_UNITS)}}]},
        }

    result = Decomposer(transport=transport).decompose("fix", repo_root=".")
    assert calls == ["mistral/codestral-latest", "mistral/codestral-latest", "cc/claude-haiku-5-5"]
    assert result.model == "cc/claude-haiku-5-5"
    assert len(result.attempts) == 3


def test_two_cheap_failures_without_ladder_success_raise_with_three_attempts() -> None:
    def transport(model_id: str, payload: Mapping[str, Any], timeout_seconds: float) -> Any:
        return {"status_code": 503, "body": {}}

    with pytest.raises(DecompositionError, match="HTTP 503"):
        Decomposer(transport=transport).decompose("fix", repo_root=".")
