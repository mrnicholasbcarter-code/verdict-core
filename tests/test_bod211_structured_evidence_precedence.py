"""Test that structured HTTP/runtime evidence outranks OpenJev signals (BOD-211)."""

import ast
import inspect
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

from verdict.autodev_routing import OpenAICompatibleEvidenceAdapter
from verdict.decision_signals.contracts import DecisionSignalSetV1
from verdict.gateway_adapter_runtime import AdapterFailureSignal
from verdict.gateway_adapters import NormalizedFailureClass


def test_normalize_failure_ignores_decision_signals():
    """Test that normalize_failure classification is independent of DecisionSignalSetV1 in scope.

    BOD-211 item 1: structured HTTP/runtime evidence (status codes, timeouts, error codes)
    outranks OpenJev signals. There is no API through which a DecisionSignalSetV1 can
    influence normalize_failure classification.
    """
    # Create a minimal mock availability surface
    # normalize_failure doesn't use the availability surface, so we just need a valid mock
    mock_surface = MagicMock()

    adapter = OpenAICompatibleEvidenceAdapter(
        mock_surface, gateway_id="test-gateway", protocol="openai.chat"
    )
    now = datetime(2026, 9, 25, 10, 0, 0, tzinfo=timezone.utc)

    # Test cases: (signal, expected_failure_class, expected_retryable)
    test_cases = [
        # 402: explicit quota exhaustion
        (
            AdapterFailureSignal(code="payment_required", status_code=402),
            NormalizedFailureClass.QUOTA,
            False,
        ),
        # 429 + insufficient_quota: quota exhaustion
        (
            AdapterFailureSignal(code="insufficient_quota", status_code=429),
            NormalizedFailureClass.QUOTA,
            False,
        ),
        # 429 + Retry-After: rate limiting
        (
            AdapterFailureSignal(code="rate_limit_exceeded", status_code=429, retry_after="30"),
            NormalizedFailureClass.RATE_LIMIT,
            True,
        ),
        # 529: overloaded
        (
            AdapterFailureSignal(code="overloaded", status_code=529),
            NormalizedFailureClass.OVERLOADED,
            True,
        ),
        # status None: transport error
        (
            AdapterFailureSignal(code="connection_error", status_code=None),
            NormalizedFailureClass.TRANSPORT,
            True,
        ),
        # timed_out=True: timeout
        (
            AdapterFailureSignal(code="timeout", timed_out=True),
            NormalizedFailureClass.TIMEOUT,
            True,
        ),
    ]

    for signal, expected_class, expected_retryable in test_cases:
        # 1. Classify without any DecisionSignalSetV1 in scope
        result_without_signals = adapter.normalize_failure(signal, now=now)
        assert result_without_signals.failure_class == expected_class
        assert result_without_signals.retryable == expected_retryable

        # 2. Create a "disagreeing" DecisionSignalSetV1 with a DIFFERENT failure_class
        # and high confidence, and put it in scope
        if expected_class == NormalizedFailureClass.QUOTA:
            disagreeing_class = NormalizedFailureClass.RATE_LIMIT
        else:
            disagreeing_class = NormalizedFailureClass.QUOTA

        # Create a disagreeing signal set in scope (unused, but tests isolation)
        _disagreeing_signal_set = DecisionSignalSetV1(
            schema_version="decision-signals/v1",
            provider="openjev",
            model="system-one",
            version="1.0.0",
            request_id="test-request-disagree",
            purpose="test",
            signals={"frontier_worthy": 0.9},
            confidence=0.99,  # Very confident, but wrong
            latency_ms=50,
            usage={"input_tokens": 10, "output_tokens": 5},
            input_digest="9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
            observed_at="2026-09-25T10:00:00Z",
            failure_class=disagreeing_class,  # DISAGREES with HTTP evidence
            mode="SHADOW",
        )

        # 3. The DecisionSignalSetV1 is now in scope, but normalize_failure
        # should produce IDENTICAL results (HTTP evidence wins)
        result_with_disagreeing_signals = adapter.normalize_failure(signal, now=now)
        assert result_with_disagreeing_signals.failure_class == expected_class
        assert result_with_disagreeing_signals.retryable == expected_retryable
        assert result_with_disagreeing_signals.failure_class == result_without_signals.failure_class
        assert result_with_disagreeing_signals.retryable == result_without_signals.retryable
        if expected_retryable and signal.retry_after:
            assert (
                result_with_disagreeing_signals.cooldown_seconds
                == result_without_signals.cooldown_seconds
            )


def test_normalize_failure_has_no_decision_signal_parameter():
    """Test that normalize_failure does not accept a decision-signal parameter.

    BOD-211 item 1: there is no API through which a DecisionSignalSetV1 can influence
    normalize_failure classification.
    """
    sig = inspect.signature(OpenAICompatibleEvidenceAdapter.normalize_failure)
    param_names = list(sig.parameters.keys())
    # Expected parameters: self, signal, now
    assert param_names == ["self", "signal", "now"], f"Unexpected parameters: {param_names}"
    # Ensure there's no parameter with "decision" or "signal_set" in the name
    for name in param_names:
        assert "decision" not in name.lower()
        assert "signal_set" not in name.lower()


def test_no_decision_signals_imports_in_core_modules():
    """Test that core routing/eligibility modules do not import from verdict.decision_signals.

    BOD-211 item 2: import-graph guard. Core modules must not import decision_signals.
    """
    core_modules = [
        "verdict/autodev_routing.py",
        "verdict/gateway_adapter_runtime.py",
        "verdict/gateway_adapters.py",
        "verdict/availability.py",
        "verdict/eligibility.py",
        "verdict/orchestration/eligibility.py",
    ]

    for module_path in core_modules:
        full_path = Path(module_path)
        assert full_path.exists(), f"Module {module_path} not found"

        with open(full_path, encoding="utf-8") as f:
            source = f.read()

        tree = ast.parse(source, filename=str(full_path))

        # Check all import statements
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("verdict.decision_signals"), (
                        f"{module_path} imports {alias.name} (decision_signals forbidden)"
                    )
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.module.startswith("verdict.decision_signals"):
                    raise AssertionError(
                        f"{module_path} imports from {node.module} (decision_signals forbidden)"
                    )


def test_decision_signals_only_in_shadow_block():
    """Test that decision_signals_data in run.py is only used inside the SHADOW block.

    BOD-211 item 3: decision_signals_data must not leak into TaskRequirements or selector.select.
    Uses an allow-list: every reference must be (a) an assignment target, (b) an is None comparison,
    or (c) the signals= argument to events.emit("decision_signals", ...). Aliases are forbidden.
    """
    run_py_path = Path("verdict/orchestration/run.py")
    assert run_py_path.exists()

    with open(run_py_path, encoding="utf-8") as f:
        source = f.read()

    tree = ast.parse(source, filename=str(run_py_path))

    # Find the plan_with_failover function (could be async)
    plan_with_failover_node = None
    for node in tree.body:
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "plan_with_failover"
        ):
            plan_with_failover_node = node
            break

    assert plan_with_failover_node is not None, "plan_with_failover not found"

    # Build parent map for the function
    parent_map = {}
    for parent in ast.walk(plan_with_failover_node):
        for child in ast.iter_child_nodes(parent):
            parent_map[child] = parent

    # Find all Name nodes referencing decision_signals_data
    decision_signals_data_nodes = []
    for node in ast.walk(plan_with_failover_node):
        if isinstance(node, ast.Name) and node.id == "decision_signals_data":
            decision_signals_data_nodes.append(node)

    # Sanity check: we should find at least one use
    assert decision_signals_data_nodes, "decision_signals_data not found in plan_with_failover"

    # Allow-list: check each use
    for node in decision_signals_data_nodes:
        allowed = False
        parent = parent_map.get(node)

        # (a) Assignment target: decision_signals_data = ... or decision_signals_data: Type = ...
        if isinstance(parent, (ast.Assign, ast.AnnAssign)):
            # Check if this node is a target
            if (isinstance(parent, ast.Assign) and node in parent.targets) or (
                isinstance(parent, ast.AnnAssign) and parent.target == node
            ):
                allowed = True

        # (b) Left side of `is None` or `is not None` comparison
        if isinstance(parent, ast.Compare):
            # Check if node is the left operand
            if parent.left == node:
                # Check if all operators are Is/IsNot and all comparators are None
                if all(isinstance(op, (ast.Is, ast.IsNot)) for op in parent.ops):
                    if all(
                        isinstance(comp, ast.Constant) and comp.value is None
                        for comp in parent.comparators
                    ):
                        allowed = True

        # (c) The value of signals= keyword in events.emit("decision_signals", ...)
        if isinstance(parent, ast.keyword) and parent.arg == "signals":
            # Check if the keyword is part of an events.emit call
            grandparent = parent_map.get(parent)
            if isinstance(grandparent, ast.Call):
                # Check if it's events.emit("decision_signals", ...)
                if (
                    isinstance(grandparent.func, ast.Attribute)
                    and grandparent.func.attr == "emit"
                    and isinstance(grandparent.func.value, ast.Name)
                    and grandparent.func.value.id == "events"
                ):
                    # Check if the first argument is "decision_signals"
                    if (
                        grandparent.args
                        and isinstance(grandparent.args[0], ast.Constant)
                        and grandparent.args[0].value == "decision_signals"
                    ):
                        allowed = True

        if not allowed:
            raise AssertionError(
                f"decision_signals_data used outside SHADOW allow-list at line {node.lineno} "
                f"(must be: assignment target, is None comparison, or events.emit signals= argument)"
            )
