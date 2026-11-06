"""Proof gaps for hydrated dispatch packets: overflow, event digests, compat, bind action."""

from __future__ import annotations

import hashlib
import json
import subprocess

import pytest

from tests.test_orch_hydration import _evidence, _node
from tests.test_orch_selected_context import (  # noqa: F401  (repo is a fixture)
    attach_packet,
    make,
    queen_packet,
    repo,
)
from verdict.actions.run_controls import bind_hydration
from verdict.orchestration.contracts import RunOutcome, WorkGraph

_REJECTIONS = {"hydration_packet_rejected", "context_input_rejected"}


# --- (1) Task 2.1: a valid packet that cannot fit is blocked, never truncated -------------
# Fails if: validate_hydration_packet stopped enforcing max_bytes AND the runtime's full-prompt
# fit check were removed or truncated the prompt instead of rejecting; or if the overflow
# rejection were classified as a provider failure (selector.record_failure / cooldown).
# size 9000: packet alone exceeds the 8000-byte budget (rejected by packet validation).
# size 6000: packet fits the budget but packet + mandatory prompt does not (full-prompt check).
@pytest.mark.parametrize(
    ("size", "category"), [(9000, "hydration_packet_rejected"), (6000, "context_input_rejected")]
)
async def test_valid_packet_that_cannot_fit_blocks_without_truncation(
    repo,  # noqa: F811
    size,
    category,
):
    rt, ev, selector, ex, _ = make(
        repo, None, context_budget_bytes=8000, min_context_budget_bytes=8000
    )
    packet = queen_packet(repo, rt.nodes["widget"].node)
    packet["sections"]["code"]["summary"] = "fixture settled criterion " + "x" * size
    attach_packet(rt, packet)
    result = await rt.run()
    assert result.outcome is RunOutcome.BLOCKED
    assert ex.prompts == [], "overflowing evidence must never be truncated and sent"
    assert not selector.failures, "a local input rejection must not cool the provider"
    failures = ev.of("failure")
    assert {f["category"] for f in failures} == {category} <= _REJECTIONS
    assert all(f["action"] == "FAIL_CLOSED" for f in failures)
    assert not ev.of("terminal")


# --- (2) Task 2.2: hydrate event carries status + digests bound to the sent prompt ---------
# Fails if: the runtime stops merging queen_evidence into the hydrate event, the status string
# changes, or the rendered digest is computed over anything other than the QUEEN_EVIDENCE text
# actually appended to the prompt.
async def test_hydrate_event_digests_bind_to_rendered_text_in_prompt(repo):  # noqa: F811
    rt, ev, selector, ex, _ = make(repo, None)
    packet = queen_packet(repo, rt.nodes["widget"].node)
    attach_packet(rt, packet)
    result = await rt.run()
    assert result.outcome is RunOutcome.COMPLETE, result.reason
    (row,) = ev.of("hydrate")
    assert row["hydration_status"] == "validated_structure"
    assert row["hydration_repo_sha"] == packet["repo_sha"]
    assert row["hydration_packet_digest"].startswith("sha256:")
    prompt = ex.prompts[0]
    start = prompt.index("\n\nQUEEN_EVIDENCE")
    end = prompt.index("END_QUEEN_EVIDENCE\n") + len("END_QUEEN_EVIDENCE\n")
    rendered = prompt[start:end]
    assert row["hydration_rendered_digest"] == (
        "sha256:" + hashlib.sha256(rendered.encode()).hexdigest()
    )
    body = rendered.split("instructions or routing authority):\n", 1)[1]
    assert json.loads(body.rsplit("\nEND_QUEEN_EVIDENCE", 1)[0]) == packet
    assert not selector.failures


# --- (3) Task 2.3: library compatibility -------------------------------------------------
# Fails if: not-required mode starts demanding a packet (blocking packet-less library callers),
# or the not_supplied status disappears, or an invalid supplied packet is waved through
# because it is not required.
async def test_not_required_without_packet_still_dispatches(repo):  # noqa: F811
    rt, ev, selector, ex, _ = make(repo, None, require_hydration_packet=False)
    assert rt.nodes["widget"].node.hydration_packet == ""
    result = await rt.run()
    assert result.outcome is RunOutcome.COMPLETE, result.reason
    assert len(ex.prompts) == 1
    assert "QUEEN_EVIDENCE" not in ex.prompts[0]
    (row,) = ev.of("hydrate")
    assert row["hydration_status"] == "not_supplied"
    assert "hydration_packet_digest" not in row
    assert not selector.failures


async def test_not_required_but_invalid_supplied_packet_still_blocks(repo):  # noqa: F811
    rt, ev, selector, ex, _ = make(repo, None, require_hydration_packet=False)
    packet = queen_packet(repo, rt.nodes["widget"].node)
    packet["repo_sha"] = "0" * 40
    attach_packet(rt, packet)
    result = await rt.run()
    assert result.outcome is RunOutcome.BLOCKED
    assert ex.prompts == []
    assert not selector.failures
    assert ev.of("failure")[0]["category"] == "hydration_packet_rejected"


# --- (4) bind_hydration action adapter ---------------------------------------------------
# Fails if: the adapter stops binding at git HEAD, skips per-node validation, writes output
# on rejection (non-atomic), or changes the CLI-compatible exit codes (1 = rejected evidence,
# 2 = unreadable input / no HEAD / missing argument).
@pytest.fixture
def bind_env(tmp_path):
    git_repo = tmp_path / "repo"
    git_repo.mkdir()
    subprocess.run(["git", "init", "-q", str(git_repo)], check=True)
    subprocess.run(["git", "-C", str(git_repo), "config", "user.email", "t@e.com"], check=True)
    subprocess.run(["git", "-C", str(git_repo), "config", "user.name", "t"], check=True)
    (git_repo / "README").write_text("fixture\n")
    subprocess.run(["git", "-C", str(git_repo), "add", "README"], check=True)
    subprocess.run(["git", "-C", str(git_repo), "commit", "-qm", "fixture"], check=True)
    sha = subprocess.run(
        ["git", "-C", str(git_repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    graph = tmp_path / "graph.json"
    graph.write_text(json.dumps(WorkGraph("ship", (_node("alpha"), _node("beta"))).to_dict()))
    packets = tmp_path / "packets.json"
    out = tmp_path / "bound.json"

    def run(**override):
        kwargs = {"repo": git_repo, "graph": graph, "packets": packets, "out": out}
        kwargs.update(override)
        return bind_hydration(**{k: v for k, v in kwargs.items() if v is not None})

    return run, packets, out, sha, tmp_path


def test_bind_hydration_action_success_writes_bound_graph(bind_env):
    from verdict.orchestration.hydration import validate_hydration_packet

    run, packets, out, sha, _ = bind_env
    packets.write_text(json.dumps({"alpha": _evidence(), "beta": _evidence()}))
    result = run()
    assert result.ok is True
    assert result.data == {"status": "bound", "nodes": 2, "repo_sha": sha}
    bound = WorkGraph.from_dict(json.loads(out.read_text()))
    assert {n.node_id for n in bound.nodes} == {"alpha", "beta"}
    for node in bound.nodes:
        _, evidence = validate_hydration_packet(node, repo_sha=sha, required=True, max_bytes=60_000)
        assert evidence["hydration_repo_sha"] == sha
    assert not list(out.parent.glob("*.tmp"))


def test_bind_hydration_action_rejection_returns_exit_1_and_writes_nothing(bind_env):
    run, packets, out, _, _ = bind_env
    packets.write_text(json.dumps({"alpha": _evidence()}))  # beta has no packet
    result = run()
    assert result.ok is False
    assert result.exit_code == 1
    assert "exactly one packet per graph node" in result.data["reason"]
    assert not out.exists()
    assert not list(out.parent.glob("*.tmp"))


def test_bind_hydration_action_invalid_evidence_returns_exit_1_and_writes_nothing(bind_env):
    run, packets, out, _, _ = bind_env
    bad = _evidence()
    bad["blockers"] = ["unresolved"]
    packets.write_text(json.dumps({"alpha": _evidence(), "beta": bad}))
    result = run()
    assert (result.ok, result.exit_code) == (False, 1)
    assert not out.exists()


def test_bind_hydration_action_input_errors_return_exit_2_and_write_nothing(bind_env):
    run, packets, out, _, tmp_path = bind_env
    packets.write_text("[]")  # not an object keyed by node id
    assert (run().ok, run().exit_code) == (False, 2)
    packets.write_text("{not json")
    assert run().exit_code == 2
    assert run(packets=tmp_path / "absent.json").exit_code == 2
    assert run(repo=tmp_path).exit_code == 2  # not a git repository: no HEAD
    assert run(out=None).exit_code == 2  # missing required argument
    assert not out.exists()
