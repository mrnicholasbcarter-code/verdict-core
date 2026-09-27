"""BOD-264: supervisor-owned root controller failover across generations.

Generation 0 runs a controller whose Prime log ends in a provider-scoped 429.
The supervisor must classify that generation with the shared failure policy,
persist the cooldown in the ladder state *before* reselection, and start
generation 1 on a different eligible controller. Selection for every
generation reads the cooldowns through the real admission evidence reader
(``verdict.admission.evidence_from_ladder_state``), so a cooled
route/provider cannot be reselected from stale state.
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.test_prime_supervisor import _git_init, _write_controller_decision, module
from verdict.admission import evidence_from_ladder_state

PROMPT = "COMPILED\n/skill:verdict-resume\n"
DIGEST = "sha256:" + hashlib.sha256(PROMPT.encode()).hexdigest()
POOL = ["cc/ctrl-a", "cc/ctrl-b", "kr/ctrl-c"]


def _cooled(ladder: Path, route: str) -> bool:
    evidence = evidence_from_ladder_state(ladder, now=datetime.now(timezone.utc))
    provider = route.split("/", 1)[0]
    keys = {f"route:{route}", f"provider:{provider}"}
    return any(o.key in keys and o.state == "cooldown" for o in evidence.observations)


def _run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gen0_log: str, *, max_restarts: int = 1
) -> tuple[Any, list[str], Path, Path]:
    m = module()
    repo = tmp_path / "repo"
    _git_init(repo)
    state = tmp_path / "state"
    state.mkdir()
    home = tmp_path / "verdict-home"
    monkeypatch.setenv("VERDICT_HOME", str(home))
    ladder = home / "orchestration-health.json"
    selected: list[str] = []

    def fake_factory(**kwargs: Any) -> Any:
        return SimpleNamespace(
            hooks=object(),
            artifacts=SimpleNamespace(
                last_compiled=SimpleNamespace(route_id="", compiled_prompt=PROMPT),
                last_admission=None,
            ),
        )

    def resolve(**kwargs: Any) -> Any:
        # Each generation re-runs selection against fresh admission evidence.
        route = next(r for r in POOL if not _cooled(ladder, r))
        provider, model = "omniroute", route
        path = _write_controller_decision(
            tmp_path / f"d-{kwargs['attempt_id']}.json", provider=provider, model=model
        )
        payload = json.loads(path.read_text())
        payload["selected_prompt_digest"] = DIGEST
        payload["selected_upstream_route"] = route
        path.write_text(json.dumps(payload))
        selected.append(route)
        persisted = m.load_persisted_authoritative_decision(path)
        mission = m.load_controller_mission(None, attempt_id=kwargs["attempt_id"])
        return m.decide_controller_launch(mission, persisted=persisted, now=kwargs.get("now"))

    def fake_run_attempt(command: list[str], cwd: Path, log: Path, *a: Any, **k: Any) -> Any:
        token = Path(command[command.index("--session-dir") + 1]).name
        if len(selected) == 1:
            log.write_text(gen0_log)
            return {"reason": "EXIT", "returncode": 1}
        # Generation 1 is only started after generation 0's receipt exists.
        assert list(state.glob("generation-failure-*.json"))
        (state / "outcome.json").write_text(
            json.dumps({"run_id": token, "status": "DONE", "reason": "resumed"})
        )
        return {"reason": "EXIT", "returncode": 0}

    monkeypatch.setattr(m, "CONTROLLER_SELECTOR", None)
    monkeypatch.setattr(m, "CONTROLLER_SELECTION_HOOKS", None)
    monkeypatch.setattr(m, "CONTROLLER_SELECTION_FACTORY", fake_factory)
    monkeypatch.setattr(m, "resolve_controller_decision", resolve)
    monkeypatch.setattr(m, "load_session_state_from_prior", lambda **k: None)
    monkeypatch.setattr(m, "run_attempt", fake_run_attempt)
    monkeypatch.setattr(m, "stop_owned_daemon", lambda *a, **k: None)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "prime_supervisor.py",
            "--repo",
            str(repo),
            "--state-dir",
            str(state),
            "--skip-identity-verify",
            "--max-restarts",
            str(max_restarts),
            "--idle-seconds",
            "30",
            "--timeout",
            "60",
        ],
    )
    monkeypatch.setenv("VERDICT_TEST_MODE", "1")
    code = m.main()
    return code, selected, state, ladder


def test_provider_429_generation_is_replaced_on_another_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = "Model request failed: [429]: All cc accounts rate limited. retry-after: 600\n"
    code, selected, state, ladder = _run(tmp_path, monkeypatch, log)

    assert code == 0
    # Generation 1 skipped the cooled provider entirely (cc/ctrl-b not chosen).
    assert selected == ["cc/ctrl-a", "kr/ctrl-c"]
    cooldowns = json.loads(ladder.read_text())["cooldowns"]
    assert cooldowns["route:cc/ctrl-a"]["category"] == "rate_limited"
    assert cooldowns["provider:cc"]["category"] == "rate_limited"
    receipts = sorted(state.glob("generation-failure-*.json"))
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text())
    assert receipt["route_id"] == "cc/ctrl-a"
    assert receipt["status_code"] == 429
    assert receipt["scope"] == "provider"
    assert receipt["cooldown_seconds"] == 600
    supervisor = json.loads((state / "supervisor.json").read_text())
    assert supervisor["status"] == "STOPPED" and supervisor["attempts"] == 2


def test_context_length_generation_writes_no_cooldown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = (
        "Model request failed: [kiro/claude-opus-5.5] [400]: Input is too long. (reset after 84h)\n"
    )
    code, selected, state, ladder = _run(tmp_path, monkeypatch, log)

    assert code == 0
    # Request-scoped: the route stays eligible, so it is reselected (after the
    # new generation compacts); nothing poisons it for 84 hours.
    assert selected == ["cc/ctrl-a", "cc/ctrl-a"]
    assert not ladder.exists()
    receipt = json.loads(next(state.glob("generation-failure-*.json")).read_text())
    assert receipt["category"] == "context_length_exceeded"
    assert receipt["scope"] == "none"
    assert receipt["cooldown"] is None


def test_restart_budget_is_bounded_and_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = "HTTP 401 invalid credentials\n"
    code, selected, state, ladder = _run(tmp_path, monkeypatch, log, max_restarts=0)

    assert code == 2
    assert selected == ["cc/ctrl-a"]
    assert json.loads((state / "supervisor.json").read_text())["status"] == "BLOCKED"
    assert json.loads(ladder.read_text())["cooldowns"]["provider:cc"]["category"] == (
        "authentication"
    )
