"""Bootstrap grammar and injected refresh/reload/consent consumers.

No projection status is inferred from refresh outcomes. Only the reloaded local
projection and Prime's separate compatibility checks can authorize a transaction.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from verdict.actions.base import ActionResult
from verdict.actions.harness_bootstrap import PrimeReadAdapter, prime_reader, restore_preview
from verdict.actions.verified_models import (
    StorePaths,
    VerifiedSnapshotAdapter,
    _lazy_live_transport,
    _wait_on_worker,
    utc_now,
)
from verdict.admission import canonical_route_id
from verdict.harness_prime_compat import exact_token, prime_selection_rows
from verdict.harness_prime_selection import load_settings, preview_selection
from verdict.orchestration.health_cache import HealthCache
from verdict.orchestration.verified_models import VerifiedModelQuery
from verdict.orchestration.verified_models_render import (
    format_verified_progress,
    render_refresh_plan,
)
from verdict.orchestration.verified_refresh import (
    RefreshConfig,
    config_from_env,
    refresh_for_consumer,
)
from verdict.tui_verified_controls import (
    ControlsError,
    VerifiedControlsController,
    parse_probe_model_list,
)


@dataclass(frozen=True)
class BootstrapArgs:
    target: str
    ids: tuple[str, ...] = ()
    restore: bool = False
    transaction_id: str | None = None
    mode: str = "native"


def parse_bootstrap_args(text: str) -> BootstrapArgs:
    tokens = text.split()
    if not tokens or tokens[0] not in {"prime", "claude"}:
        raise ControlsError("choose prime or claude; use /help bootstrap")
    target, args = tokens[0], tokens[1:]
    if any(token.startswith("-") for token in text.replace(",", " ").split()):
        raise ControlsError("options are not model ids; use /help bootstrap")
    if target == "prime" and args[:1] == ["restore"]:
        transaction = None
        if len(args) > 2 or (len(args) == 2 and not args[1].startswith("transaction=")):
            raise ControlsError("prime restore accepts only transaction=<id>")
        if len(args) == 2:
            transaction = args[1].split("=", 1)[1]
            if len(transaction) != 32 or any(c not in "0123456789abcdef" for c in transaction):
                raise ControlsError("invalid transaction id")
        return BootstrapArgs(target, restore=True, transaction_id=transaction)
    mode = "native"
    model_tokens = []
    for token in args:
        if token.startswith("mode=") and target == "claude":
            mode = token[5:]
            if mode not in {"native", "openai-side-path"}:
                raise ControlsError("Claude report modes: native or openai-side-path")
        elif "=" in token or token in {"apply", "select", "restore"}:
            raise ControlsError(
                "Claude is read-only (NEEDS_OWNER BOD-102); Prime accepts exact ids"
            )
        else:
            model_tokens.append(token)
    ids = parse_probe_model_list(" ".join(model_tokens)) if model_tokens else []
    if any(
        not exact_token(rid) or re.search(r"(?i)(sk-|bearer|api[_-]?key|token[=:]|https?://)", rid)
        for rid in ids
    ):
        raise ControlsError("exact ids only; no patterns or unsafe identities")
    return BootstrapArgs(target, tuple(ids), mode=mode)


def _load_rows(adapter: VerifiedSnapshotAdapter) -> list[dict[str, Any]]:
    first = adapter.load(VerifiedModelQuery(page_size=200))
    rows = list(first.to_dict()["rows"])
    for page in range(2, first.page_count + 1):
        rows.extend(adapter.load(VerifiedModelQuery(page=page, page_size=200)).to_dict()["rows"])
    return rows


def _refresh_selected(
    adapter: VerifiedSnapshotAdapter,
    ids: Sequence[str],
    *,
    consumer: str,
    controller: VerifiedControlsController,
    config: RefreshConfig,
    clock: Callable[[], datetime],
    transport: Callable[..., Any] | None,
    live: bool,
    interactive: bool,
) -> list[dict[str, Any]]:
    from verdict.actions.registry import run_action

    # Metadata was loaded once before choosing. Scoping changes only this consumer.
    adapter._load_metadata()
    wanted = {canonical_route_id(rid) for rid in ids}
    scoped = replace(
        adapter,
        inventory_rows=[
            row
            for row in adapter.inventory_rows or []
            if canonical_route_id(str(row.get("id", ""))) in wanted
        ],
    )
    rows = _load_rows(scoped)
    snapshot = scoped.refresh_snapshot(scoped.load(VerifiedModelQuery(page_size=200)))
    # Include all involved rows even if the presentation page was smaller.
    from verdict.orchestration.verified_refresh import RowInput

    snapshot = replace(
        snapshot,
        rows=tuple(
            RowInput(
                route_id=row["route_id"],
                provider=row["provider"],
                status=row["status"],
                capacity_class=row.get("capacity_class") or "unknown",
                refreshable=bool(row.get("refreshable")),
                refresh_reason=row.get("refresh_reason"),
            )
            for row in rows
        ),
    )
    controller.write(
        "Bounded prepaid refresh only; it may consume prepaid quota. Waiting before reload."
    )
    outcome = controller.refresh(
        snapshot,
        consumer=consumer,
        needed_ids=ids,
        config=config,
        cache=HealthCache(scoped.paths.health_cache),
        clock=clock,
        transport=transport or (_lazy_live_transport(scoped.gateway) if live else None),
        on_progress=lambda event: controller.write(format_verified_progress(event)),
    )
    if getattr(outcome, "outcome", None) == "cancelled":
        raise ControlsError("refresh cancelled; no selection apply")
    rows = _load_rows(scoped)  # Outcomes are never proof.
    # Metered/unknown execution requires a separate digest-bound spend decision.
    manual_ids = [
        row["route_id"]
        for row in rows
        if row["status"] != "VERIFIED"
        and str(row.get("capacity_class", "")).upper() in {"METERED", "UNKNOWN"}
    ]
    if manual_ids and interactive:
        current = replace(
            snapshot,
            rows=tuple(
                RowInput(
                    route_id=row["route_id"],
                    provider=row["provider"],
                    status=row["status"],
                    capacity_class=row.get("capacity_class") or "unknown",
                    refreshable=bool(row.get("refreshable")),
                    refresh_reason=row.get("refresh_reason"),
                )
                for row in rows
            ),
            generation=scoped.refresh_snapshot(scoped.load(VerifiedModelQuery())).generation,
        )
        plan = run_action(
            "models.refresh.plan",
            {
                "snapshot_rows": rows,
                "needed_ids": manual_ids,
                "consumer": consumer,
                "gateway_origin": scoped.gateway,
                "evidence_generation": current.generation,
                "include_metered": True,
                "caps": {
                    "max_routes": config.max_routes,
                    "max_requests": config.max_requests,
                    "wall_seconds": config.wall_seconds,
                    "concurrency": config.concurrency,
                },
                "now": clock(),
            },
        )
        if plan.ok and plan.data.get("routes"):
            controller.write(render_refresh_plan(plan.data))
            if controller.confirm("Spend quota for this exact manual refresh plan?").granted:
                # Re-read exact current rows AND generation; consent never
                # relabels capacity or carries forward vanished/blocked ids.
                latest_view = scoped.load(VerifiedModelQuery(page_size=200))
                current = scoped.refresh_snapshot(latest_view)

                def execute(snap: Any, **extra: Any) -> ActionResult:
                    return run_action(
                        "models.refresh.execute",
                        {
                            "confirmed": True,
                            "plan": plan.data,
                            "snapshot": snap,
                            "cache": HealthCache(scoped.paths.health_cache),
                            "transport": transport
                            or (_lazy_live_transport(scoped.gateway) if live else None),
                            "clock": clock,
                            "now": clock(),
                            "on_progress": extra.get("on_progress"),
                            "cancel": extra.get("cancel"),
                        },
                    )

                execution = replace(
                    controller, run_refresh=_wait_on_worker(execute, live=live)
                ).refresh(
                    current,
                    consumer=consumer,
                    needed_ids=manual_ids,
                    config=config,
                    authorized=True,
                    on_progress=lambda event: controller.write(format_verified_progress(event)),
                )
                if not execution.ok or execution.data.get("outcome") == "cancelled":
                    raise ControlsError("manual refresh refused or cancelled; no apply")
                rows = _load_rows(scoped)
    return rows


def consume_prime(
    args: BootstrapArgs,
    *,
    gateway: str,
    reader: PrimeReadAdapter | None = None,
    adapter: VerifiedSnapshotAdapter | None = None,
    config: RefreshConfig | None = None,
    read_line: Callable[[str], Any] = input,
    write: Callable[[str], None] = lambda _s: None,
    clock: Callable[[], datetime] = utc_now,
    run_refresh: Callable[..., Any] = refresh_for_consumer,
    transport: Callable[..., Any] | None = None,
    cancel: Callable[[], bool] | None = None,
    live: bool = False,
    preview_only: bool = False,
    interactive: bool = True,
    on_projection: Callable[[Mapping[str, Any]], None] | None = None,
) -> ActionResult:
    """Discover, refresh/reload, preview, default-No, refresh/reload, guarded apply."""
    from verdict.actions.registry import run_action

    reader = reader or prime_reader(gateway)
    config = config or config_from_env()
    controller = VerifiedControlsController(
        read_line, write, _wait_on_worker(run_refresh, live=live), cancel
    )
    try:
        dependencies = reader.dependencies()
        if not dependencies.discovery.installed:
            raise ControlsError("Prime binary missing")
        settings = load_settings(reader.paths.settings)
        if args.restore:
            preview = restore_preview(reader, args.transaction_id)
            data = preview.to_dict()
            write(json.dumps(data, indent=2))
            if preview.refusals or preview_only or not interactive:
                return ActionResult(
                    data=data, ok=not preview.refusals, exit_code=2 if preview.refusals else 0
                )
            if not controller.confirm("Restore prior scope (not newly verified)?").granted:
                return ActionResult(data={**data, "status": "cancelled"})
            return run_action(
                "harness.prime.restore.apply",
                {
                    "preview": preview,
                    "confirmed": True,
                    "expected_post_digest": preview.expected_post_digest,
                    "paths": reader.paths,
                    "clock": clock,
                    "reload_dependencies": reader.dependencies,
                },
            )
        adapter = adapter or VerifiedSnapshotAdapter(
            gateway,
            StorePaths.defaults(),
            clock=clock,
            deadline=time.monotonic() + config.wall_seconds,
        )
        ids = args.ids
        if not ids:
            page = adapter.load(VerifiedModelQuery())
            overlay = prime_selection_rows(
                dependencies.models_doc,
                page.to_dict()["rows"],
                context=dependencies.discovery,
                now=clock(),
            )
            write(
                json.dumps(
                    {
                        "target": str(reader.paths.settings),
                        "rows": [row.to_dict() for row in overlay],
                    },
                    indent=2,
                )
            )
            if not interactive:
                return ActionResult(
                    data={
                        "schema": "verdict.prime-selection/v1",
                        "status": "choose_exact_ids",
                        "rows": [row.to_dict() for row in overlay],
                    }
                )
            try:
                answer = read_line("Exact model ids (comma/space separated; empty cancels): ")
                if not answer:
                    return ActionResult(data={"status": "cancelled"})
                ids = parse_bootstrap_args("prime " + str(answer)).ids
            except (EOFError, KeyboardInterrupt):
                return ActionResult(data={"status": "cancelled"})
        rows = _refresh_selected(
            adapter,
            ids,
            consumer="picker",
            controller=controller,
            config=config,
            clock=clock,
            transport=transport,
            live=live,
            interactive=interactive,
        )
        if on_projection is not None:
            from verdict.tui_completion_snapshot import local_projection

            on_projection(local_projection(adapter, now=clock()))
        preview = preview_selection(
            settings, rows, selected_ids=ids, dependencies=dependencies, now=clock()
        )
        data = preview.to_dict()
        write(json.dumps(data, indent=2))
        if preview.refusals or preview_only or not interactive:
            return ActionResult(
                data=data, ok=not preview.refusals, exit_code=2 if preview.refusals else 0
            )
        if not controller.confirm("Apply this exact interactive scope?").granted:
            return ActionResult(data={**data, "status": "cancelled"})
        _refresh_selected(
            adapter,
            ids,
            consumer="selection",
            controller=controller,
            config=config,
            clock=clock,
            transport=transport,
            live=live,
            interactive=interactive,
        )
        if on_projection is not None:
            from verdict.tui_completion_snapshot import local_projection

            on_projection(local_projection(adapter, now=clock()))
        # Core refuses changed config/dependencies/diff or expired original consent.
        # Under lock the callback reads local evidence only; metadata stays frozen.
        adapter._load_metadata()
        return run_action(
            "harness.prime.select.apply",
            {
                "preview": preview,
                "expected_pre_digest": preview.pre_digest,
                "confirmed": True,
                "paths": reader.paths,
                "reload_rows": lambda: _load_rows(adapter),
                "reload_dependencies": reader.dependencies,
                "clock": clock,
            },
        )
    except (ValueError, OSError, TypeError, KeyError):
        return ActionResult(
            data={
                "error": "bootstrap refused: unsafe, changed or missing local input; use /help bootstrap"
            },
            ok=False,
            exit_code=2,
        )
    except (EOFError, KeyboardInterrupt):
        return ActionResult(data={"status": "cancelled"})
