"""Eligibility golden capture shim — called by test_actions_json_golden.py as subprocess.

Monkeypatches fetch_inventory and fetch_connections at the module level before
importing verdict.cli, so the eligibility command sees the faked 2-row inventory.
Writes JSON stdout to stdout; exits with the same code as the CLI.
"""

from __future__ import annotations

import sys
from unittest import mock

# The test harness sets PYTHONPATH=<PR branch root>; this shim always runs from PR branch.
# But when capturing goldens, it was run with PYTHONPATH=<origin/main root>.
# Either way, we rely on whatever is on sys.path.

FAKE_ROWS = [
    {"id": "kr/claude-opus-5", "object": "model", "context_length": 200000, "owned_by": "kr"},
    {"id": "kr/claude-sonnet-5", "object": "model", "context_length": 200000, "owned_by": "kr"},
]
FAKE_CONNECTIONS = [
    {
        "id": "conn-kr-1",
        "name": "kr",
        "provider": "kr",
        "status": "active",
        "plan_label": "max",
        "rate_limited_until": None,
        "import_free_only": False,
    }
]


def _fake_fetch_inventory(gateway: str, *, api_key: object, timeout: float = 30) -> list:
    return FAKE_ROWS


def _fake_fetch_connections(gateway: str, *, api_key: object, timeout: float = 30) -> list:
    return FAKE_CONNECTIONS


def main() -> int:
    import verdict.orchestration.run as _orch_run
    import verdict.subagent_selection as _sel

    _orig_probe = _sel.openai_health_probe

    def _fake_probe(*a: object, **kw: object):  # type: ignore[override]
        return lambda c: None

    with (
        mock.patch.object(_orch_run, "fetch_inventory", _fake_fetch_inventory),
        mock.patch.object(_orch_run, "fetch_connections", _fake_fetch_connections),
    ):
        _sel.openai_health_probe = _fake_probe  # type: ignore[assignment]
        try:
            import verdict.cli as _cli

            sys.argv = ["verdict", "eligibility", "--json", "--no-pager"]
            try:
                _cli.main()
                return 0
            except SystemExit as exc:
                code = exc.code
                if isinstance(code, int):
                    return code
                return 0 if code is None else 1
        finally:
            _sel.openai_health_probe = _orig_probe  # type: ignore[assignment]


if __name__ == "__main__":
    sys.exit(main())
