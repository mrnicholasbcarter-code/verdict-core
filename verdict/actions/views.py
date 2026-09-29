"""Read-only routing and context view actions.

Both actions load an existing view model and return its machine projection.
They never recompute selection, ranking, budget, or eligibility, and the
inventory path never probes.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

from verdict.actions.base import ActionResult
from verdict.orchestration.routing_render import filter_candidates, paginate

_USAGE = 2
_NOT_FOUND = 3
_MAX_PAGE_SIZE = 200


def _resolve_run(value: str, runs_dir: str) -> Path:
    path = Path(value)
    return path if path.is_dir() else Path(runs_dir) / value


def _missing(kind: str, where: str) -> ActionResult:
    return ActionResult(data={"error": f"no {kind} at {where}"}, ok=False, exit_code=_NOT_FOUND)


def _select_node(view: Any, node: str | None, *, attr: str) -> tuple[Any, ActionResult | None]:
    """Narrow a view to one node. Returns (view, error)."""
    if not node:
        return view, None
    # Views are frozen dataclasses: build a narrowed copy instead of mutating.
    if attr == "evaluations":
        kept = [item for item in view.evaluations if item.node_id == node]
        if not kept:
            return view, _missing("node", node)
        return _replace_field(view, "evaluations", kept), None
    kept_nodes = [item for item in view.nodes if item.node_id == node]
    if not kept_nodes:
        return view, _missing("node", node)
    return _replace_field(view, "nodes", kept_nodes), None


def _replace_field(view: Any, name: str, items: list[Any]) -> Any:
    """Copy *view* with one sequence field replaced, keeping its container type."""
    import dataclasses

    current = getattr(view, name)
    value: Any = tuple(items) if isinstance(current, tuple) else items
    if dataclasses.is_dataclass(view) and not isinstance(view, type):
        return dataclasses.replace(view, **{name: value})
    setattr(view, name, value)
    return view


def _routing_filters(params: dict[str, Any]) -> dict[str, Any]:
    """Renderer filter kwargs. Page size stays inside the renderer's cap."""
    page_size = int(params.get("page_size") or 25)
    return {
        "state": params.get("state") or None,
        "provider": params.get("provider") or None,
        "text": params.get("search") or None,
        "page": int(params.get("page") or 0),
        "page_size": max(1, min(page_size, _MAX_PAGE_SIZE)),
    }


def _action_routing_view(**kwargs: Any) -> ActionResult:
    """Recorded routing view, or a read-only inventory view that never probes."""
    import verdict.orchestration.routing_render as routing_render
    import verdict.orchestration.routing_view as routing_view_mod

    routing_json = routing_render.routing_json
    routing_view = routing_view_mod.routing_view
    routing_view_from_inventory = routing_view_mod.routing_view_from_inventory

    if kwargs.get("inventory"):
        view = routing_view_from_inventory(
            dict(kwargs.get("task_profile") or {}),
            probe=False,
            inventory=kwargs.get("inventory_source"),
            gateway=str(kwargs.get("gateway") or "http://localhost:20128"),
        )
    else:
        run = kwargs.get("run")
        if not run:
            return ActionResult(
                data={"error": "routing requires a run id or --inventory"},
                ok=False,
                exit_code=_USAGE,
            )
        run_dir = _resolve_run(str(run), str(kwargs.get("runs_dir") or ".verdict/runs"))
        events = run_dir / "events.jsonl"
        if not events.is_file():
            return _missing("run", str(run_dir))
        view = routing_view(run_dir)

    view, error = _select_node(view, kwargs.get("node") or None, attr="evaluations")
    if error is not None:
        return error
    filters = _routing_filters(kwargs)
    # The JSON projection has no filter arguments, so narrow a copy with the
    # same pure filter the text renderer uses. Ranking stays untouched.
    narrowed = []
    for evaluation in view.evaluations:
        if evaluation.candidates is None:
            narrowed.append(evaluation)
            continue
        rows = filter_candidates(
            evaluation.candidates,
            evaluation,
            state=filters["state"],
            provider=filters["provider"],
            text=filters["text"],
        )
        page_rows, _index, _pages = paginate(
            rows, page=filters["page"], page_size=filters["page_size"]
        )
        narrowed.append(dataclasses.replace(evaluation, candidates=page_rows))
    view = dataclasses.replace(view, evaluations=narrowed)
    return ActionResult(data={"payload": routing_json(view), "view": view, "filters": filters})


def _action_context_view(**kwargs: Any) -> ActionResult:
    """Recorded context budget view. Never re-reads source files."""
    import verdict.orchestration.context_render as context_render
    import verdict.orchestration.context_view as context_view_mod

    context_json = context_render.context_json
    context_view = context_view_mod.context_view

    run = kwargs.get("run")
    if not run:
        return ActionResult(
            data={"error": "context requires a run id or run directory"}, ok=False, exit_code=_USAGE
        )
    run_dir = _resolve_run(str(run), str(kwargs.get("runs_dir") or ".verdict/runs"))
    events = run_dir / "events.jsonl"
    if not events.is_file():
        return _missing("run", str(run_dir))
    view = context_view(run_dir)
    view, error = _select_node(view, kwargs.get("node") or None, attr="nodes")
    if error is not None:
        return error
    return ActionResult(data={"payload": context_json(view), "view": view})
