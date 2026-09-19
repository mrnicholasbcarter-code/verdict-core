"""Post-execution outcome receipts: the only producer of measured serving spend (BOD-117).

The decision log (:mod:`verdict.logger`) is written *before* the upstream call,
so it cannot know what the execution cost. This module records, per attempt,
what the gateway actually reported after the response came back:

* ``observed_cost_usd`` — from ``X-OmniRoute-Response-Cost`` only. No header,
  no number: the request is *unmeasured*, never ``$0``.
* token counts — from ``X-OmniRoute-Tokens-In/Out`` or, failing that, the
  response body ``usage``; token counts never become a price.
* ``completed_with`` — the model this attempt was sent to (gateway header
  when present, else the attempt's model), ``execution_id``, ``status_code``.

Records go to a sibling of the decision log (``verdict-outcomes.jsonl`` next
to ``verdict-decisions.jsonl``) and join back on ``request_id``. Nothing here
is ever inferred from a model name.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

OUTCOME_RECORD_KIND = "outcome"
OUTCOME_SCHEMA_VERSION = 1

COST_HEADER = "x-omniroute-response-cost"
TOKENS_IN_HEADER = "x-omniroute-tokens-in"
TOKENS_OUT_HEADER = "x-omniroute-tokens-out"
CACHE_HIT_HEADER = "x-omniroute-cache-hit"
MODEL_HEADERS = ("x-omniroute-model", "x-omniroute-completed-with")
EXECUTION_ID_HEADERS = ("x-omniroute-request-id", "x-request-id")

HeaderItems = Iterable[tuple[str, str]] | Mapping[str, str]


def outcome_log_path(decision_log_path: Path | str) -> Path:
    """``verdict-decisions.jsonl`` → ``verdict-outcomes.jsonl``; else ``<stem>-outcomes.jsonl``."""
    path = Path(decision_log_path)
    stem = path.stem
    if stem.endswith("-decisions"):
        stem = stem[: -len("-decisions")] + "-outcomes"
    elif stem == "decisions":
        stem = "outcomes"
    else:
        stem = f"{stem}-outcomes"
    return path.with_name(f"{stem}{path.suffix or '.jsonl'}")


def _lower_headers(headers: HeaderItems) -> dict[str, str]:
    items = headers.items() if isinstance(headers, Mapping) else headers
    return {str(key).lower(): str(value) for key, value in items}


def _non_negative_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        parsed = float(value)
        return parsed if math.isfinite(parsed) and parsed >= 0 else None
    if isinstance(value, str):
        try:
            parsed = float(value.strip())
        except ValueError:
            return None
        return parsed if math.isfinite(parsed) and parsed >= 0 else None
    return None


def _non_negative_int(value: object) -> int | None:
    parsed = _non_negative_float(value)
    if parsed is None or not parsed.is_integer():
        return None
    return int(parsed)


def _body_usage(body: bytes | None) -> Mapping[str, Any] | None:
    if not body:
        return None
    try:
        decoded = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    usage = decoded.get("usage") if isinstance(decoded, Mapping) else None
    return usage if isinstance(usage, Mapping) else None


def observe_execution(*, headers: HeaderItems, body: bytes | None) -> dict[str, Any]:
    """Extract observed cost/usage/identity from an upstream response. Never estimates."""
    lowered = _lower_headers(headers)

    cost = _non_negative_float(lowered.get(COST_HEADER))
    tokens_in = _non_negative_int(lowered.get(TOKENS_IN_HEADER))
    tokens_out = _non_negative_int(lowered.get(TOKENS_OUT_HEADER))
    tokens_source: str | None = None
    if tokens_in is not None or tokens_out is not None:
        tokens_source = "x-omniroute-tokens-in/out"
    else:
        usage = _body_usage(body)
        if usage is not None:
            # Chat Completions names these prompt_/completion_tokens; the
            # Responses API names them input_/output_tokens.
            tokens_in = _non_negative_int(usage.get("prompt_tokens", usage.get("input_tokens")))
            tokens_out = _non_negative_int(
                usage.get("completion_tokens", usage.get("output_tokens"))
            )
            if tokens_in is not None or tokens_out is not None:
                tokens_source = "body.usage"
    tokens_total = (
        (tokens_in or 0) + (tokens_out or 0)
        if tokens_in is not None or tokens_out is not None
        else None
    )

    cache_raw = lowered.get(CACHE_HIT_HEADER)
    cache_hit: bool | None = None
    if cache_raw is not None:
        cache_hit = cache_raw.strip().lower() in {"1", "true", "yes", "hit"}

    completed_with = next(
        (lowered[name].strip() for name in MODEL_HEADERS if lowered.get(name, "").strip()), None
    )
    execution_id = next(
        (lowered[name].strip() for name in EXECUTION_ID_HEADERS if lowered.get(name, "").strip()),
        None,
    )
    return {
        "observed_cost_usd": cost,
        "cost_source": COST_HEADER if cost is not None else None,
        "observed_tokens_in": tokens_in,
        "observed_tokens_out": tokens_out,
        "observed_tokens_total": tokens_total,
        "tokens_source": tokens_source,
        "cache_hit": cache_hit,
        "completed_with_source": "header" if completed_with else None,
        "completed_with_header": completed_with,
        "execution_id": execution_id,
    }


def build_outcome_record(
    *,
    request_id: str,
    model: str,
    status_code: int,
    attempt: int,
    surface: str,
    headers: HeaderItems,
    body: bytes | None,
) -> dict[str, Any]:
    """One row per upstream attempt, correlated to the decision by ``request_id``."""
    observed = observe_execution(headers=headers, body=body)
    completed_with = observed.pop("completed_with_header") or model
    completed_with_source = observed.pop("completed_with_source") or "attempt.model"
    return {
        "record": OUTCOME_RECORD_KIND,
        "schema_version": OUTCOME_SCHEMA_VERSION,
        "ts": datetime.now(timezone.utc).isoformat(),
        "request_id": request_id,
        "attempt": attempt,
        "surface": surface,
        "status_code": status_code,
        "completed_with": completed_with,
        "completed_with_source": completed_with_source,
        **observed,
    }


def log_outcome(decision_log_path: Path | str | None, record: Mapping[str, Any]) -> None:
    """Append an outcome record beside the decision log. Never raises; no path, no write."""
    if not decision_log_path or not str(decision_log_path).strip():
        return
    try:
        target = outcome_log_path(decision_log_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(dict(record), ensure_ascii=False) + "\n")
    except Exception:
        pass  # Never disrupt serving because a receipt could not be written.


def _iter_outcome_rows(path: Path) -> Iterable[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, Mapping) and row.get("record") == OUTCOME_RECORD_KIND:
                yield dict(row)


def is_outcome_log(path: Path | str) -> bool:
    """True when ``path`` is an outcome log (by name or by its first record)."""
    target = Path(path)
    if target.stem.endswith("-outcomes") or target.stem == "outcomes":
        return True
    if not target.is_file():
        return False
    try:
        with target.open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    return False
                return isinstance(row, Mapping) and row.get("record") == OUTCOME_RECORD_KIND
    except OSError:
        return False
    return False


def load_outcomes(decision_log_path: Path | str) -> dict[str, dict[str, Any]]:
    """All outcome receipts per ``request_id``.

    Each value is ``{"final": <highest attempt>, "attempts": [<every attempt>]}``.
    ``final`` is the response the client received (identity, status);
    ``attempts`` is what serving this request actually cost, retries included.
    """
    target = outcome_log_path(decision_log_path)
    if not target.is_file():
        return {}
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in _iter_outcome_rows(target):
        request_id = row.get("request_id")
        if isinstance(request_id, str) and request_id:
            grouped.setdefault(request_id, []).append(row)
    outcomes: dict[str, dict[str, Any]] = {}
    for request_id, rows in grouped.items():
        rows.sort(key=lambda item: int(item.get("attempt", 0)))
        outcomes[request_id] = {"final": rows[-1], "attempts": rows}
    return outcomes


def measured_spend_for(
    decision: Mapping[str, Any],
    outcomes: Mapping[str, Mapping[str, Any]],
    *,
    decision_rows: Iterable[Mapping[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Join a decision row to its outcome receipts; ``None`` when spend cannot be measured.

    Spend is the **sum of every billed attempt** for the request — a retry the
    gateway charged for is still money spent serving this decision. When the
    same ``request_id`` appears on more than one decision row (client-supplied
    ids are sanitized, not made unique) the receipt cannot be attributed to any
    one of them, so all of them are excluded rather than each being credited.
    """
    request_id = decision.get("request_id")
    if not isinstance(request_id, str) or request_id not in outcomes:
        return None
    if decision_rows is not None:
        duplicates = sum(1 for row in decision_rows if row.get("request_id") == request_id)
        if duplicates > 1:
            return None
    attempts = outcomes[request_id].get("attempts") or []
    billed = [
        (cost, _non_negative_int(row.get("observed_tokens_total")), row.get("cost_source"))
        for row in attempts
        if (cost := _non_negative_float(row.get("observed_cost_usd"))) is not None
    ]
    if not billed:
        return None
    tokens = [count for _cost, count, _source in billed if count is not None]
    headers = sorted({str(source) for _cost, _count, source in billed if source})
    return {
        "observed_cost_usd": round(sum(cost for cost, _count, _source in billed), 10),
        "observed_tokens_total": sum(tokens) if tokens else None,
        "billed_attempts": len(billed),
        "cost_source": (
            f"outcome receipt ({', '.join(headers) or 'unknown header'})"
            if len(billed) == 1
            else f"outcome receipts ({', '.join(headers) or 'unknown header'}, {len(billed)} attempts)"
        ),
    }


def ambiguous_request_ids(decision_rows: Iterable[Mapping[str, Any]]) -> set[str]:
    """Client-supplied ids that appear on more than one decision row."""
    seen: dict[str, int] = {}
    for row in decision_rows:
        request_id = row.get("request_id")
        if isinstance(request_id, str) and request_id:
            seen[request_id] = seen.get(request_id, 0) + 1
    return {request_id for request_id, count in seen.items() if count > 1}


__all__ = [
    "CACHE_HIT_HEADER",
    "COST_HEADER",
    "OUTCOME_RECORD_KIND",
    "OUTCOME_SCHEMA_VERSION",
    "TOKENS_IN_HEADER",
    "TOKENS_OUT_HEADER",
    "ambiguous_request_ids",
    "build_outcome_record",
    "is_outcome_log",
    "load_outcomes",
    "log_outcome",
    "measured_spend_for",
    "observe_execution",
    "outcome_log_path",
]
