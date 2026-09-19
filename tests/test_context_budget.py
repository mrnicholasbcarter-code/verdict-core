"""Proof tests for Context Budget Governor (BOD-125)."""

from __future__ import annotations

import json

import pytest

from verdict.context_budget import (
    BudgetCandidateLimit,
    BudgetUnit,
    ContextBudgetError,
    ContextBudgetGovernor,
    estimate_unit_tokens,
)


def _unit(
    unit_id: str,
    source_class: str,
    content: str,
    *,
    priority: str = "optional",
    value_score: float = 0.5,
    token_count: int | None = None,
    token_count_kind: str = "estimated",
    provenance_uri: str = "urn:fixture",
) -> BudgetUnit:
    return BudgetUnit(
        unit_id=unit_id,
        source_class=source_class,  # type: ignore[arg-type]
        priority=priority,  # type: ignore[arg-type]
        content=content,
        token_count=token_count,
        token_count_kind=token_count_kind,  # type: ignore[arg-type]
        provenance_uri=f"{provenance_uri}:{unit_id}",
        value_score=value_score,
    )


def test_total_context_accounting_fixture() -> None:
    """Proof 1: every source class appears in the account ledger."""
    gov = ContextBudgetGovernor()
    units = (
        _unit("sys", "system_harness", "You are Verdict.", priority="mandatory", value_score=1.0),
        _unit("mcp", "mcp_tools", '{"tools":[{"name":"search"}]}', value_score=0.8),
        _unit("task", "task_spec", "AC: keep mandatory proof fact.", priority="mandatory"),
        _unit("code", "repo_code", "def pack(): ...", value_score=0.6),
        _unit("docs", "docs", "# ADR-001", value_score=0.4),
        _unit("mem", "memory", "prior decision: use pack compiler", value_score=0.3),
        _unit("hist", "conversation_history", "user: continue", value_score=0.2),
        _unit("proof", "proof_verification", "REQUIRED_FACT=keep-me", priority="mandatory"),
        _unit("hand", "execution_handoff", "wave=3 worker=B", value_score=0.1),
    )
    limit = BudgetCandidateLimit(
        candidate_id="openrouter/free/test",
        context_limit=8000,
        output_reserve=200,
        reasoning_reserve=100,
        tool_call_reserve=150,
    )
    account = gov.account(units, limit)
    assert account.context_limit == 8000
    assert account.usable_input_budget == 8000 - 200 - 100 - 150
    assert account.reserved_total == 450
    by_class = {row.source_class: row for row in account.per_source}
    expected = {
        "system_harness",
        "mcp_tools",
        "task_spec",
        "repo_code",
        "docs",
        "memory",
        "conversation_history",
        "proof_verification",
        "execution_handoff",
        "reserved_output",
        "reserved_reasoning",
        "reserved_tool_calls",
    }
    assert expected <= set(by_class)
    assert account.total_content_tokens == sum(
        estimate_unit_tokens(u) for u in units if estimate_unit_tokens(u) is not None
    )
    assert account.digest == gov.account(units, limit).digest


def test_oversized_context_stays_under_candidate_limit() -> None:
    """Proof 2: allocation never exceeds candidate usable input budget."""
    gov = ContextBudgetGovernor()
    units = (
        _unit("task", "task_spec", "do the work", priority="mandatory", value_score=1.0),
        _unit("proof", "proof_verification", "FACT=alpha", priority="mandatory", value_score=1.0),
        _unit("noise-a", "docs", "N" * 2000, value_score=0.1),
        _unit("noise-b", "memory", "M" * 2000, value_score=0.05),
        _unit("noise-c", "conversation_history", "H" * 2000, value_score=0.01),
    )
    limit = BudgetCandidateLimit(
        candidate_id="cheap/small",
        context_limit=500,
        output_reserve=50,
        reasoning_reserve=25,
        tool_call_reserve=25,
    )
    receipt = gov.allocate(units, limit)
    assert receipt.fits is True
    assert receipt.used_tokens <= receipt.usable_input_budget
    assert receipt.used_tokens + receipt.reserved_total <= limit.context_limit
    assert receipt.omitted


def test_mandatory_facts_survive_trimming() -> None:
    """Proof 3: mandatory proof/task units cannot silently disappear."""
    gov = ContextBudgetGovernor()
    fact = "MANDATORY_PROOF_FACT_7f3a"
    units = (
        _unit("task", "task_spec", "Solve with proof.", priority="mandatory", value_score=1.0),
        _unit("proof", "proof_verification", f"keep {fact}", priority="mandatory", value_score=1.0),
        _unit("bulk", "docs", "optional noise " * 400, value_score=0.0),
        _unit("hist", "conversation_history", "chatter " * 400, value_score=0.0),
    )
    limit = BudgetCandidateLimit(
        candidate_id="mid/window",
        context_limit=400,
        output_reserve=40,
        reasoning_reserve=20,
        tool_call_reserve=20,
    )
    receipt = gov.allocate(units, limit)
    included_text = "\n".join(u.content or "" for u in receipt.included)
    assert fact in included_text
    omitted_ids = {o.unit_id for o in receipt.omitted}
    assert "proof" not in omitted_ids
    assert "task" not in omitted_ids
    assert "bulk" in omitted_ids or "hist" in omitted_ids


def test_noisy_mcp_tool_surface_visible_as_context_cost() -> None:
    """Proof 4: MCP/tool schemas are first-class budget consumers."""
    gov = ContextBudgetGovernor()
    noisy_schema = json.dumps(
        {
            "tools": [
                {"name": f"tool_{i}", "description": "x" * 80, "parameters": {"a": "b" * 40}}
                for i in range(12)
            ]
        }
    )
    units = (
        _unit("task", "task_spec", "call tools carefully", priority="mandatory"),
        _unit("mcp", "mcp_tools", noisy_schema, priority="mandatory", value_score=0.9),
    )
    limit = BudgetCandidateLimit(
        candidate_id="tools/heavy",
        context_limit=10_000,
        output_reserve=100,
        reasoning_reserve=50,
        tool_call_reserve=200,
    )
    account = gov.account(units, limit)
    mcp_row = next(row for row in account.per_source if row.source_class == "mcp_tools")
    assert mcp_row.token_count is not None and mcp_row.token_count > 200
    assert mcp_row.unit_count == 1
    receipt = gov.allocate(units, limit)
    assert any(u.unit_id == "mcp" for u in receipt.included)
    assert receipt.per_source_used["mcp_tools"] == mcp_row.token_count


def test_cheaper_candidate_viable_after_high_value_packing() -> None:
    """Proof 5: smaller window can fit after dropping low-value optional."""
    gov = ContextBudgetGovernor()
    units = (
        _unit("sys", "system_harness", "harness", priority="mandatory", value_score=1.0),
        _unit("task", "task_spec", "ship feature", priority="mandatory", value_score=1.0),
        _unit("proof", "proof_verification", "AC-1", priority="mandatory", value_score=1.0),
        _unit("gold", "repo_code", "critical symbol graph", value_score=0.95),
        _unit("noise", "docs", "Z" * 3000, value_score=0.05),
    )
    expensive = BudgetCandidateLimit(
        candidate_id="premium/large",
        context_limit=4000,
        output_reserve=100,
        reasoning_reserve=50,
        tool_call_reserve=50,
    )
    cheap = BudgetCandidateLimit(
        candidate_id="free/small",
        context_limit=600,
        output_reserve=50,
        reasoning_reserve=25,
        tool_call_reserve=25,
    )
    raw_total = sum(estimate_unit_tokens(u) or 0 for u in units)
    assert raw_total > cheap.usable_input_budget

    premium_receipt = gov.allocate(units, expensive)
    cheap_receipt = gov.allocate(units, cheap)
    assert premium_receipt.fits is True
    assert cheap_receipt.fits is True
    assert cheap_receipt.used_tokens <= cheap.usable_input_budget
    assert any(o.unit_id == "noise" for o in cheap_receipt.omitted)
    assert any(u.unit_id == "gold" for u in cheap_receipt.included)

    viability = gov.evaluate_candidates(units, (expensive, cheap))
    assert viability["premium/large"].fits is True
    assert viability["free/small"].fits is True
    assert viability["free/small"].used_tokens < raw_total


def test_budget_receipt_explains_included_and_omitted() -> None:
    """Proof 6: receipt names included/omitted units with reasons."""
    gov = ContextBudgetGovernor()
    units = (
        _unit("keep", "task_spec", "must keep", priority="mandatory", value_score=1.0),
        _unit("drop", "memory", "low value recall", value_score=0.0),
    )
    limit = BudgetCandidateLimit(
        candidate_id="explain/me",
        context_limit=80,
        output_reserve=10,
        reasoning_reserve=5,
        tool_call_reserve=5,
    )
    # Force omit by making drop huge relative to budget.
    units = (units[0], _unit("drop", "memory", "low value " * 200, value_score=0.0))
    receipt = gov.allocate(units, limit)
    assert receipt.candidate_id == "explain/me"
    assert {u.unit_id for u in receipt.included} == {"keep"}
    omitted = {o.unit_id: o for o in receipt.omitted}
    assert "drop" in omitted
    assert omitted["drop"].reason == "input_budget_exhausted"
    assert omitted["drop"].source_class == "memory"
    payload = receipt.to_dict()
    assert "included" in payload and "omitted" in payload
    assert receipt.digest == ContextBudgetGovernor().allocate(units, limit).digest


def test_no_secret_leakage_in_diagnostic_output() -> None:
    """Proof 7: diagnostics redact credentials and never echo secrets."""
    gov = ContextBudgetGovernor()
    secret = "sk-abcdefghijklmnopqrstuvwxyz0123456789"
    units = (
        _unit(
            "leak", "memory", f"api_key={secret} bearer TOKEN", priority="optional", value_score=0.5
        ),
        _unit(
            "ok",
            "task_spec",
            "normal task text",
            priority="mandatory",
            value_score=1.0,
            provenance_uri="https://user:p@ss@example.com/path?token=abc",
        ),
    )
    limit = BudgetCandidateLimit(
        candidate_id="safe/diag",
        context_limit=2000,
        output_reserve=50,
        reasoning_reserve=25,
        tool_call_reserve=25,
    )
    receipt = gov.allocate(units, limit)
    diagnostic = gov.diagnose(receipt)
    blob = json.dumps(diagnostic, sort_keys=True)
    assert secret not in blob
    assert "api_key=" not in blob
    assert "p@ss" not in blob
    assert "token=abc" not in blob
    # Secret-bearing unit must be excluded, not summarized with raw content.
    assert all(u.unit_id != "leak" for u in receipt.included)
    assert any(o.unit_id == "leak" and "secret" in o.reason for o in receipt.omitted)


def test_unknown_token_counts_stay_unknown() -> None:
    """Unknown sizes remain estimated/unknown — never fabricated exact counts."""
    gov = ContextBudgetGovernor()
    units = (
        BudgetUnit(
            unit_id="known",
            source_class="task_spec",
            priority="mandatory",
            content="hello",
            token_count=2,
            token_count_kind="exact",
            provenance_uri="urn:known",
            value_score=1.0,
        ),
        BudgetUnit(
            unit_id="mystery",
            source_class="docs",
            priority="optional",
            content=None,
            token_count=None,
            token_count_kind="unknown",
            provenance_uri="urn:mystery",
            value_score=0.2,
        ),
    )
    limit = BudgetCandidateLimit(
        candidate_id="unk",
        context_limit=1000,
        output_reserve=10,
        reasoning_reserve=10,
        tool_call_reserve=10,
    )
    account = gov.account(units, limit)
    mystery = next(u for u in account.units if u.unit_id == "mystery")
    assert mystery.token_count is None
    assert mystery.token_count_kind == "unknown"
    receipt = gov.allocate(units, limit)
    # Unknown-size optional cannot be included; recorded with explicit reason.
    assert any(
        o.unit_id == "mystery" and o.reason == "token_count_unknown" for o in receipt.omitted
    )


def test_mandatory_overflow_is_explicit_failure() -> None:
    """Mandatory that cannot fit raises — never silently dropped."""
    gov = ContextBudgetGovernor()
    units = (
        _unit("huge", "proof_verification", "P" * 4000, priority="mandatory", value_score=1.0),
    )
    limit = BudgetCandidateLimit(
        candidate_id="tiny",
        context_limit=100,
        output_reserve=20,
        reasoning_reserve=10,
        tool_call_reserve=10,
    )
    with pytest.raises(ContextBudgetError, match="mandatory") as exc:
        gov.allocate(units, limit)
    assert exc.value.code == "mandatory_overflow"
