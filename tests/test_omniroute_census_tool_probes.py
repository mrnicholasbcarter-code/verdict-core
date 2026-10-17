"""Census carries tool probes; prove-at-rest stays untouched (spec v6.1 section 5)."""

from __future__ import annotations

import inspect

from verdict import census_passports, prove_at_rest


def test_census_runs_tool_probes_and_prove_at_rest_untouched() -> None:
    sig = inspect.signature(census_passports.run_census)
    assert "tool_probe_interval_s" in sig.parameters
    assert "context_probe_max_per_tick" in sig.parameters
    assert "agentic_max_routes" not in sig.parameters
    assert hasattr(prove_at_rest, "agentic_candidates")
