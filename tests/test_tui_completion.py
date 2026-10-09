from datetime import datetime

import pytest
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document

from verdict.tui_completion import (
    ArgumentSpec,
    CommandSpec,
    CompletionSnapshot,
    VerdictCompleter,
    complete,
)


@pytest.fixture
def sample_commands():
    return [
        CommandSpec(
            name="eligibility",
            description="Check model eligibility",
            syntax="/eligibility [status] [provider=..] [search=..] [page=..] [refresh]",
            arguments=[
                ArgumentSpec("status", "choice", False, choices=["all", "verified", "stale"]),
                ArgumentSpec("provider", "field", False),
                ArgumentSpec("search", "field", False),
                ArgumentSpec("page", "field", False),
                ArgumentSpec("refresh", "flag", False),
            ],
        ),
        CommandSpec(
            name="probe",
            description="Probe models",
            syntax="/probe <ids>",
            arguments=[ArgumentSpec("ids", "models", True)],
        ),
        CommandSpec(
            name="bootstrap",
            description="Bootstrap harness",
            syntax="/bootstrap <target> [ids...] [options]",
            arguments=[
                ArgumentSpec("target", "choice", True, choices=["prime", "claude"]),
                ArgumentSpec("value", "models", False),  # simplified for now
                ArgumentSpec("mode", "choice", False, choices=["native", "openai-side-path"]),
            ],
        ),
        CommandSpec(
            name="trace",
            description="Trace run",
            syntax="/trace <run_id>",
            arguments=[ArgumentSpec("run_id", "run", True)],
        ),
        CommandSpec(
            name="routing",
            description="Routing info",
            syntax="/routing <run_id>",
            arguments=[ArgumentSpec("run_id", "run", True)],
        ),
        CommandSpec(
            name="context",
            description="Context info",
            syntax="/context <run_id>",
            arguments=[ArgumentSpec("run_id", "run", True)],
        ),
        CommandSpec(
            name="watch",
            description="Watch run",
            syntax="/watch <run_id>",
            arguments=[ArgumentSpec("run_id", "run", True)],
        ),
        CommandSpec(
            name="run-receipt",
            description="Run receipt",
            syntax="/run-receipt <run_id>",
            arguments=[ArgumentSpec("run_id", "run", True)],
        ),
        CommandSpec(
            name="receipt",
            description="Receipt info",
            syntax="/receipt <run_id>",
            arguments=[ArgumentSpec("run_id", "run", True)],
        ),
    ]


@pytest.fixture
def sample_snapshot():
    return CompletionSnapshot(
        schema="v1",
        generated_at="2023-01-01T00:00:00Z",
        run_rows=[
            {"run": "run-123", "outcome": "success", "age_s": 10},
            {"run": "run-456", "outcome": "failed", "age_s": 60},
        ],
        model_rows=[
            {"id": "omniroute/gpt-4", "name": "GPT-4", "status": "VERIFIED"},
            {"id": "omniroute/claude-3", "name": "Claude 3", "status": "STALE"},
            {"id": "omniroute/llama-3", "name": "Llama 3", "status": "UNVERIFIED"},
        ],
        provider_names=["omniroute"],
        harness_targets=["prime"],
        modes=["native"],
        source_errors=[],
    )


def test_complete_empty_string(sample_commands, sample_snapshot):
    res = complete("", commands=sample_commands, snapshot=sample_snapshot, now=datetime.now())
    assert len(res) > 0
    assert res[0].text.startswith("/")
    assert res[0].kind == "command"


def test_complete_command_prefix(sample_commands, sample_snapshot):
    # Typo: /elegibility -> /eligibility
    res = complete("/eli", commands=sample_commands, snapshot=sample_snapshot, now=datetime.now())
    assert any(c.display == "/eligibility" for c in res)


def test_complete_model_ids(sample_commands, sample_snapshot):
    # /probe <ids>
    res = complete(
        "/probe ", commands=sample_commands, snapshot=sample_snapshot, now=datetime.now()
    )
    assert any("gpt-4" in c.display for c in res)
    assert any("claude-3" in c.display for c in res)


def test_complete_model_labels(sample_commands, sample_snapshot):
    res = complete(
        "/probe ", commands=sample_commands, snapshot=sample_snapshot, now=datetime.now()
    )
    # Claude 3 is STALE
    claude = next(c for c in res if "claude-3" in c.display)
    assert "[stale]" in claude.description

    # Llama 3 is UNVERIFIED
    llama = next(c for c in res if "llama-3" in c.display)
    assert "[unverified]" in llama.description


def test_complete_run_ids(sample_commands, sample_snapshot):
    # /trace <run_id>
    res = complete(
        "/trace ", commands=sample_commands, snapshot=sample_snapshot, now=datetime.now()
    )
    assert any("run-123" in c.display for c in res)
    assert any("run-456" in c.display for c in res)


def test_complete_choices(sample_commands, sample_snapshot):
    # /eligibility [status]
    res = complete(
        "/eligibility ", commands=sample_commands, snapshot=sample_snapshot, now=datetime.now()
    )
    assert any("verified" in c.display for c in res)
    assert any("stale" in c.display for c in res)


def test_no_completion_for_flags_as_models(sample_commands, sample_snapshot):
    # /probe --probe should not suggest models
    res = complete(
        "/probe --", commands=sample_commands, snapshot=sample_snapshot, now=datetime.now()
    )
    # Should not be model candidates
    assert not any(c.kind == "model" for c in res)


def test_prompt_toolkit_adapter(sample_commands, sample_snapshot):
    completer = VerdictCompleter(sample_commands, sample_snapshot, datetime.now())
    doc = Document("/probe ")
    event = CompleteEvent()

    completions = list(completer.get_completions(doc, event))
    assert len(completions) > 0
    assert completions[0].text.startswith("omniroute")


def test_performance_bounds(sample_commands):
    # Create a huge snapshot
    huge_model_rows = [
        {"id": f"mod-{i}", "name": f"Model {i}", "status": "VERIFIED"} for i in range(10000)
    ]
    snapshot = CompletionSnapshot(
        schema="v1",
        generated_at="...",
        run_rows=[],
        model_rows=huge_model_rows,
        provider_names=[],
        harness_targets=[],
        modes=[],
        source_errors=[],
    )

    import time

    start = time.perf_counter()
    complete("/probe ", commands=sample_commands, snapshot=snapshot, now=datetime.now())
    duration = (time.perf_counter() - start) * 1000
    assert duration < 50


def test_determinism(sample_commands, sample_snapshot):
    text = "/probe "
    res1 = complete(text, commands=sample_commands, snapshot=sample_snapshot, now=datetime.now())
    res2 = complete(text, commands=sample_commands, snapshot=sample_snapshot, now=datetime.now())
    assert res1 == res2
