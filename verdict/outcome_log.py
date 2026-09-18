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
        return float(value) if value >= 0 else None
    if isinstance(value, str):
        try:
            parsed = float(value.strip())
        except ValueError:
            return None
        return parsed if parsed >= 0 else None
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
            tokens_in = _non_negative_int(usage.get("prompt_tokens"))
            tokens_out = _non_negative_int(usage.get("completion_tokens"))
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


def load_outcomes(decision_log_path: Path | str) -> dict[str, dict[str, Any]]:
    """Latest outcome per ``request_id`` (the final attempt is the one the client got)."""
    target = outcome_log_path(decision_log_path)
    if not target.is_file():
        return {}
    outcomes: dict[str, dict[str, Any]] = {}
    with target.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, Mapping) or row.get("record") != OUTCOME_RECORD_KIND:
                continue
            request_id = row.get("request_id")
            if not isinstance(request_id, str) or not request_id:
                continue
            previous = outcomes.get(request_id)
            if previous is None or int(row.get("attempt", 0)) >= int(previous.get("attempt", 0)):
                outcomes[request_id] = dict(row)
    return outcomes


def measured_spend_for(
    decision: Mapping[str, Any], outcomes: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any] | None:
    """Join a decision row to its outcome receipt; ``None`` when spend was not observed."""
    request_id = decision.get("request_id")
    if not isinstance(request_id, str) or request_id not in outcomes:
        return None
    outcome = outcomes[request_id]
    cost = _non_negative_float(outcome.get("observed_cost_usd"))
    if cost is None:
        return None
    return {
        "observed_cost_usd": cost,
        "observed_tokens_total": _non_negative_int(outcome.get("observed_tokens_total")),
        "cost_source": f"outcome receipt ({outcome.get('cost_source') or 'unknown header'})",
    }


__all__ = [
    "CACHE_HIT_HEADER",
    "COST_HEADER",
    "OUTCOME_RECORD_KIND",
    "OUTCOME_SCHEMA_VERSION",
    "TOKENS_IN_HEADER",
    "TOKENS_OUT_HEADER",
    "build_outcome_record",
    "load_outcomes",
    "log_outcome",
    "measured_spend_for",
    "observe_execution",
    "outcome_log_path",
]
