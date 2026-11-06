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


def bind_hydration(**kwargs: Any) -> ActionResult:
    """Bind queen hydration packets to a graph at the repository HEAD (local files only)."""
    import json
    import subprocess

    from verdict.orchestration.contracts import OrchestrationError, WorkGraph
    from verdict.orchestration.hydration import bind_hydration_packets

    try:
        repo = Path(kwargs["repo"])
        graph_path = Path(kwargs["graph"])
        packets_path = Path(kwargs["packets"])
        out = Path(kwargs["out"])
    except KeyError as exc:
        return ActionResult(data={"reason": f"{exc.args[0]} is required"}, ok=False, exit_code=2)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=False
    )
    sha = head.stdout.strip()
    if head.returncode != 0 or not sha:
        return ActionResult(data={"reason": f"{repo} has no HEAD"}, ok=False, exit_code=2)
    try:
        graph = WorkGraph.from_dict(json.loads(graph_path.read_text()))
        packets = json.loads(packets_path.read_text())
        if not isinstance(packets, dict):
            return ActionResult(
                data={"reason": "packets must be a JSON object keyed by node id"},
                ok=False,
                exit_code=2,
            )
        bound = bind_hydration_packets(
            graph, packets, repo_sha=sha, max_bytes=int(kwargs.get("max_bytes", 60_000))
        )
        tmp = out.with_suffix(out.suffix + ".tmp")
        tmp.write_text(json.dumps(bound.to_dict(), indent=2) + "\n")
        tmp.replace(out)
    except OrchestrationError as exc:
        return ActionResult(data={"reason": str(exc)}, ok=False, exit_code=1)
    except (OSError, ValueError) as exc:
        return ActionResult(data={"reason": str(exc)}, ok=False, exit_code=2)
    return ActionResult(data={"status": "bound", "nodes": len(bound.nodes), "repo_sha": sha})
