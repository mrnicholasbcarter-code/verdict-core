"""S2-A regressions: live harness gate, provider-spread probing, unservable routes."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from tests.test_orch_eligibility import NOW, REQ, FakeProbe, by_route, conn, make_ladder, row
from verdict.orchestration.cli import prime_visibility
from verdict.orchestration.contracts import EligibilityStage
from verdict.orchestration.eligibility import EligibilityLadder
from verdict.subagent_selection import HealthResult, classify_probe_status

PAYMENT = HealthResult(healthy=False, category="payment_required", status_code=402)


def _models_json(path: Path, ids: list[str]) -> Path:
    path.write_text(
        json.dumps({"providers": {"omniroute": {"models": [{"id": i} for i in ids]}}}),
        encoding="utf-8",
    )
    return path


class TestLiveHarnessGate:
    def test_live_inventory_admits_route_absent_from_models_json(self, tmp_path: Path) -> None:
        registry = _models_json(tmp_path / "models.json", ["cc/claude-sonnet-5"])
        gate = prime_visibility(registry, live_ids=["cc/claude-sonnet-5", "cc/claude-new-6"])
        ladder, _ = make_ladder(
            tmp_path,
            [row("cc/claude-new-6", owned_by="claude")],
            [conn("claude")],
            harness_visible=gate,
        )
        v = ladder.evaluate(REQ, now=NOW)[0]
        assert v.failed_stage is None
        assert v.reason not in {"not_harness_visible", "harness_inventory_unavailable"}

    def test_live_gate_still_denies_ids_the_gateway_does_not_list(self, tmp_path: Path) -> None:
        gate = prime_visibility(tmp_path / "missing.json", live_ids=["cc/other"])
        ladder, _ = make_ladder(
            tmp_path,
            [row("cc/claude-new-6", owned_by="claude")],
            [conn("claude")],
            harness_visible=gate,
        )
        v = ladder.evaluate(REQ, now=NOW)[0]
        assert v.failed_stage is EligibilityStage.ENTITLED
        assert v.reason == "not_harness_visible"

    def test_models_json_is_fallback_when_no_live_inventory(self, tmp_path: Path) -> None:
        registry = _models_json(tmp_path / "models.json", ["cc/claude-sonnet-5"])
        gate = prime_visibility(registry)
        assert gate("cc/claude-sonnet-5") and not gate("cc/claude-new-6")
        assert gate.source == "models.json"

    def test_no_inventory_and_no_models_json_fails_closed(self, tmp_path: Path) -> None:
        gate = prime_visibility(tmp_path / "absent.json", live_ids=None)
        assert gate is not None  # never "no gate": that would admit everything
        ladder, probe = make_ladder(
            tmp_path,
            [row("cc/claude-sonnet-5", owned_by="claude")],
            [conn("claude")],
            harness_visible=gate,
        )
        selected, verdicts = ladder.select(REQ, now=NOW)
        assert selected is None
        assert probe.calls == []
        v = verdicts[0]
        assert v.failed_stage is EligibilityStage.ENTITLED
        assert v.reason == "harness_inventory_unavailable"


class TestProviderSpreadProbing:
    def _rows(self) -> list[dict[str, object]]:
        # Provider A ranks first (preferred) and owns most routes.
        rows = [row(f"aa/model-{i:02d}", owned_by="alpha") for i in range(7)]
        rows += [row(f"bb/model-{i:02d}", owned_by="beta") for i in range(3)]
        return rows

    def test_payment_failures_on_one_provider_do_not_starve_selection(self, tmp_path: Path) -> None:
        rows = self._rows()
        probe = FakeProbe({str(r["id"]): PAYMENT for r in rows if r["owned_by"] == "alpha"})
        ladder, _ = make_ladder(
            tmp_path,
            rows,
            [conn("alpha"), conn("beta")],
            probe,
            prefer_providers=("alpha", "beta"),
            max_probes_per_select=2,
        )
        selected, verdicts = ladder.select(REQ, now=NOW)
        assert selected is not None and selected.provider == "beta"
        assert len(probe.calls) <= 2
        assert sum(1 for c in probe.calls if c.startswith("aa/")) == 1
        skipped = [
            v for v in verdicts if v.route_id.startswith("aa/") and v.route_id not in probe.calls
        ]
        assert skipped and all(v.reason == "cooldown:provider" for v in skipped)
        assert all(v.cooldown_until for v in skipped)

    def test_round_robin_across_providers_within_budget(self, tmp_path: Path) -> None:
        rows = self._rows()
        timeout = HealthResult(healthy=False, category="timeout")
        probe = FakeProbe({str(r["id"]): timeout for r in rows if r["owned_by"] == "alpha"})
        ladder, _ = make_ladder(
            tmp_path,
            rows,
            [conn("alpha"), conn("beta")],
            probe,
            prefer_providers=("alpha", "beta"),
            max_probes_per_select=8,
        )
        selected, _ = ladder.select(REQ, now=NOW)
        # timeouts are route-scoped, yet beta is still reached on the 2nd probe
        assert selected is not None and selected.route_id == "bb/model-00"
        assert probe.calls == ["aa/model-00", "bb/model-00"]
        assert selected.rank is not None and selected.rank > 0  # rank stays the global rank

    def test_provider_cooldown_persists_to_fresh_selector(self, tmp_path: Path) -> None:
        rows = self._rows()
        probe = FakeProbe({str(r["id"]): PAYMENT for r in rows if r["owned_by"] == "alpha"})
        conns = [conn("alpha"), conn("beta")]
        first, _ = make_ladder(tmp_path, rows, conns, probe, prefer_providers=("alpha", "beta"))
        first.select(REQ, now=NOW)
        state = json.loads((tmp_path / "state.json").read_text())
        assert state["cooldowns"]["provider:alpha"]["category"] == "payment_required"

        probe2 = FakeProbe()
        fresh = EligibilityLadder(
            rows, conns, probe2, tmp_path / "state.json", prefer_providers=("alpha", "beta")
        )
        selected, verdicts = fresh.select(REQ, now=NOW + timedelta(minutes=5))
        assert selected is not None and selected.provider == "beta"
        assert not any(c.startswith("aa/") for c in probe2.calls)
        alpha = [v for v in verdicts if v.provider == "alpha"]
        assert alpha and all(v.failed_stage is not None for v in alpha)
        untouched = [v for v in alpha if v.route_id not in probe.calls]
        assert untouched and all(v.reason == "cooldown:provider" for v in untouched)


class TestUnservable:
    def test_live_catalog_400_is_unservable_with_route_cooldown(self, tmp_path: Path) -> None:
        body = (
            '{"error":{"message":"Model kr/x-xhigh is not available in the active live catalog"}}'
        )
        result = classify_probe_status(400, body=body)
        assert result.healthy is False and result.category == "unservable"
        rows = [row("kr/x-xhigh", owned_by="kiro"), row("kr/y", owned_by="kiro")]
        probe = FakeProbe({"kr/x-xhigh": result})
        ladder, _ = make_ladder(tmp_path, rows, [conn("kiro")], probe)
        selected, verdicts = ladder.select(REQ, now=NOW)
        assert selected is not None and selected.route_id == "kr/y"
        v = by_route(verdicts)["kr/x-xhigh"]
        assert v.failed_stage is EligibilityStage.HEALTHY
        assert v.reason == "unservable"
        assert v.cooldown_until == (NOW + timedelta(hours=6)).isoformat()
        # route-scoped only: the sibling route of the same provider is untouched
        state = json.loads((tmp_path / "state.json").read_text())
        assert "provider:kiro" not in state["cooldowns"]
        later = by_route(ladder.evaluate(REQ, now=NOW + timedelta(hours=1)))["kr/x-xhigh"]
        assert later.failed_stage is not None

    def test_plain_400_stays_unsupported(self) -> None:
        assert classify_probe_status(400, body="bad request").category == "unsupported"
