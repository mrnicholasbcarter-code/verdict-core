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
