"""Thin shared action adapters for the orchestration domain control service."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from verdict.actions.base import ActionResult


def _submit(kind: str, kwargs: dict[str, Any]) -> ActionResult:
    from verdict.orchestration.controls import ControlError, RunControl

    value = kwargs.get("run_dir") or kwargs.get("run_id")
    if not value:
        return ActionResult(
            data={"reason": "run id or directory is required"}, ok=False, exit_code=2
        )
    run_dir = Path(value)
    if not run_dir.is_absolute() and not run_dir.is_dir():
        run_dir = Path(kwargs.get("runs_dir", ".verdict/runs")) / run_dir
    try:
        request = RunControl(run_dir).submit(
            kind, node_id=kwargs.get("node_id"), requested_by=kwargs.get("requested_by", "operator")
        )
    except (ControlError, OSError, ValueError) as exc:
        return ActionResult(data={"reason": str(exc)}, ok=False, exit_code=2)
    return ActionResult(data={"status": "queued", "request": request.to_dict()})


def cancel_run(**kwargs: Any) -> ActionResult:
    return _submit("cancel_run", kwargs)


def cancel_node(**kwargs: Any) -> ActionResult:
    return _submit("cancel_node", kwargs)


def retry_node(**kwargs: Any) -> ActionResult:
    return _submit("retry_node", kwargs)
