"""Probe golden capture shim — injects a deterministic fake transport."""

from __future__ import annotations

import sys
from collections.abc import Mapping
from typing import Any


class _FakeTransport:
    """Returns a valid 200 + 1-token assistant completion so the probe succeeds."""

    def __call__(self, model_id: str, payload: Mapping[str, Any], timeout_seconds: float) -> Any:
        return {
            "status_code": 200,
            "body": {
                "choices": [
                    {"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                "model": model_id,
            },
        }


def _fake_action_probe(**kwargs: Any) -> Any:
    from verdict.actions.base import ActionResult
    from verdict.actions.helpers import probe_result_payload
    from verdict.probes import ProbePolicy, ProbeRunner

    models: list[str] = kwargs["models"]
    timeout: float = kwargs.get("timeout", 20.0)
    run = ProbeRunner(ProbePolicy(timeout_seconds=timeout)).run_with_diagnostics(
        models, _FakeTransport(), live=False, consented=False, provider="fixture"
    )
    results = [probe_result_payload(obs) for obs in run.observations]
    data = {"diagnostics": run.diagnostics.to_dict(), "results": results}
    all_ok = all(e.get("ok") for e in results)
    return ActionResult(data=data, ok=all_ok, exit_code=0 if all_ok else 1)


def main() -> int:
    import verdict.actions.registry as _reg
    import verdict.cli as _cli

    existing_spec = _reg._ACTIONS["probe"][0]
    _reg._ACTIONS["probe"] = (existing_spec, _fake_action_probe)
    sys.argv = ["verdict", "probe", "kr/claude-sonnet-5-thinking", "--json"]
    try:
        _cli.main()
        return 0
    except SystemExit as exc:
        code = exc.code
        return code if isinstance(code, int) else (0 if code is None else 1)


if __name__ == "__main__":
    sys.exit(main())
