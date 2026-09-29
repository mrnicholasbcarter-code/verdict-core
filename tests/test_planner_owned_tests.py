"""Tests for planner verification-file ownership fixup (BOD-263 / BOD-203).

The planner must add test files referenced in verification_command to owned_files
so the runtime ownership barrier does not reject correct worker edits.
"""

from __future__ import annotations

import json

from verdict.orchestration.contracts import NodeKind, WorkNode
from verdict.orchestration.planner import (
    _test_files_from_argv,
    ensure_verification_files_owned,
    parse_plan,
)

# ---- _test_files_from_argv --------------------------------------------------


class TestTestFilesFromArgv:
    """Extract test file paths from pytest-style argv."""

    def test_bare_test_file(self) -> None:
        assert _test_files_from_argv(["python3", "-m", "pytest", "tests/test_foo.py"]) == [
            "tests/test_foo.py"
        ]

    def test_test_file_with_selector(self) -> None:
        argv = ["pytest", "-q", "tests/test_recovery.py::TestReroute::test_cooldown"]
        assert _test_files_from_argv(argv) == ["tests/test_recovery.py"]

    def test_multiple_test_files(self) -> None:
        argv = ["pytest", "tests/test_a.py", "tests/test_b.py"]
        assert _test_files_from_argv(argv) == ["tests/test_a.py", "tests/test_b.py"]

    def test_k_selector_only(self) -> None:
        """A -k selector names tests, not files; no files should be extracted."""
        argv = ["pytest", "-k", "test_something"]
        assert _test_files_from_argv(argv) == []

    def test_no_test_files(self) -> None:
        argv = ["ruff", "check", "verdict/"]
        assert _test_files_from_argv(argv) == []

    def test_deduplication(self) -> None:
        argv = ["pytest", "tests/test_x.py::A", "tests/test_x.py::B"]
        assert _test_files_from_argv(argv) == ["tests/test_x.py"]

    def test_nested_test_path(self) -> None:
        argv = ["pytest", "tests/integration/test_deep.py"]
        assert _test_files_from_argv(argv) == ["tests/integration/test_deep.py"]


# ---- ensure_verification_files_owned ----------------------------------------


def _make_node(
    node_id: str = "n1",
    owned: tuple[str, ...] = ("verdict/recovery.py",),
    verify: tuple[str, ...] = ("pytest", "-q", "tests/test_recovery.py"),
    kind: NodeKind = NodeKind.IMPLEMENT,
) -> WorkNode:
    return WorkNode(
        node_id=node_id,
        objective="do work",
        kind=kind,
        owned_files=owned,
        verification_command=verify,
    )


class TestEnsureVerificationFilesOwned:
    """Test the deterministic post-planning fixup."""

    def test_adds_missing_test_file(self) -> None:
        """The exact BOD-263 defect: owned_files lacks the verification test file."""
        node = _make_node(
            owned=("verdict/recovery.py", "verdict/eligibility.py", "verdict/runtime.py"),
            verify=("pytest", "-q", "tests/test_orch_recovery.py"),
        )
        assert not node.owns("tests/test_orch_recovery.py")
        [fixed] = ensure_verification_files_owned([node])
        assert fixed.owns("tests/test_orch_recovery.py")
        # Original owned files still present
        assert fixed.owns("verdict/recovery.py")
        assert fixed.owns("verdict/eligibility.py")

    def test_already_owned_is_noop(self) -> None:
        node = _make_node(
            owned=("verdict/foo.py", "tests/test_foo.py"), verify=("pytest", "tests/test_foo.py")
        )
        [result] = ensure_verification_files_owned([node])
        assert result is node  # identity — no copy needed

    def test_non_implement_unchanged(self) -> None:
        node = WorkNode(
            node_id="review-1",
            objective="review",
            kind=NodeKind.REVIEW,
            verification_command=("pytest", "tests/test_foo.py"),
        )
        [result] = ensure_verification_files_owned([node])
        assert result is node

    def test_no_verification_command_unchanged(self) -> None:
        node = WorkNode(node_id="integrate-1", objective="integrate", kind=NodeKind.INTEGRATE)
        [result] = ensure_verification_files_owned([node])
        assert result is node

    def test_multiple_test_files_added(self) -> None:
        node = _make_node(
            owned=("verdict/x.py",), verify=("pytest", "tests/test_a.py", "tests/test_b.py")
        )
        [fixed] = ensure_verification_files_owned([node])
        assert fixed.owns("tests/test_a.py")
        assert fixed.owns("tests/test_b.py")
        assert fixed.owns("verdict/x.py")

    def test_selector_resolves_to_file(self) -> None:
        node = _make_node(
            owned=("verdict/x.py",), verify=("pytest", "tests/test_x.py::TestClass::test_method")
        )
        [fixed] = ensure_verification_files_owned([node])
        assert fixed.owns("tests/test_x.py")

    def test_preserves_node_fields(self) -> None:
        """All original fields survive the fixup."""
        node = WorkNode(
            node_id="n1",
            objective="do work",
            kind=NodeKind.IMPLEMENT,
            story="a story",
            depends_on=("dep1",),
            owned_files=("verdict/a.py",),
            required_context=("docs/ref.md",),
            acceptance=("thing works",),
            verification_command=("pytest", "tests/test_a.py"),
            barrier="",
            risk="high",
            required_capabilities=("tools", "reasoning"),
            coding=True,
            reasoning=True,
            min_context_tokens=64_000,
        )
        [fixed] = ensure_verification_files_owned([node])
        assert fixed.node_id == "n1"
        assert fixed.story == "a story"
        assert fixed.depends_on == ("dep1",)
        assert fixed.risk == "high"
        assert fixed.required_capabilities == ("tools", "reasoning")
        assert fixed.reasoning is True
        assert fixed.min_context_tokens == 64_000
        assert fixed.owns("tests/test_a.py")

    def test_plan_without_tests_unchanged(self) -> None:
        """A verification_command that references no test files leaves owned_files alone."""
        node = _make_node(owned=("verdict/x.py",), verify=("ruff", "check", "verdict/x.py"))
        [result] = ensure_verification_files_owned([node])
        assert result is node


# ---- parse_plan integration -------------------------------------------------


class TestParsePlanIntegration:
    """Ensure parse_plan applies the verification ownership fixup."""

    def _plan_json(self, owned_files: list[str], verify: list[str]) -> str:
        return json.dumps(
            {
                "nodes": [
                    {
                        "node_id": "impl-1",
                        "objective": "implement feature",
                        "kind": "implement",
                        "story": "",
                        "depends_on": [],
                        "owned_files": owned_files,
                        "required_context": [],
                        "acceptance": ["tests pass"],
                        "verification_command": verify,
                        "barrier": "",
                        "risk": "low",
                        "required_capabilities": ["tools"],
                        "coding": True,
                        "reasoning": False,
                        "min_context_tokens": 32000,
                    },
                    {
                        "node_id": "integrate",
                        "objective": "integrate",
                        "kind": "integrate",
                        "story": "",
                        "depends_on": ["impl-1"],
                        "owned_files": [],
                        "required_context": [],
                        "acceptance": [],
                        "verification_command": ["pytest", "-q"],
                        "barrier": "",
                        "risk": "low",
                        "required_capabilities": ["tools"],
                        "coding": False,
                        "reasoning": False,
                        "min_context_tokens": 32000,
                    },
                ]
            }
        )

    def test_parse_plan_adds_test_to_owned(self) -> None:
        """End-to-end: parse_plan fixes a node whose verification references an unowned test."""
        text = self._plan_json(
            owned_files=["verdict/recovery.py"], verify=["pytest", "-q", "tests/test_recovery.py"]
        )
        graph = parse_plan(text, "test goal")
        impl = next(n for n in graph.nodes if n.node_id == "impl-1")
        assert impl.owns("tests/test_recovery.py")
        assert impl.owns("verdict/recovery.py")

    def test_parse_plan_no_test_no_change(self) -> None:
        """parse_plan does not alter owned_files when verification has no test paths."""
        text = self._plan_json(
            owned_files=["verdict/recovery.py"], verify=["ruff", "check", "verdict/recovery.py"]
        )
        graph = parse_plan(text, "test goal")
        impl = next(n for n in graph.nodes if n.node_id == "impl-1")
        assert impl.owned_files == ("verdict/recovery.py",)

    def test_parse_plan_already_owned(self) -> None:
        """parse_plan does not duplicate an already-owned test file."""
        text = self._plan_json(
            owned_files=["verdict/recovery.py", "tests/test_recovery.py"],
            verify=["pytest", "tests/test_recovery.py"],
        )
        graph = parse_plan(text, "test goal")
        impl = next(n for n in graph.nodes if n.node_id == "impl-1")
        # No duplicates
        assert impl.owned_files.count("tests/test_recovery.py") == 1
