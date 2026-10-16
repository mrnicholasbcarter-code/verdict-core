"""Verdict memory plugin for Hermes Agent (stdlib only; runs inside Hermes's venv).

Each hook forwards a bounded payload as JSON to the Verdict command recorded in
``config.json`` (written by the Verdict installer) and never raises into Hermes.
Only ``pre_llm_call`` returns anything: ``{"context": ...}`` when Verdict recalled
memory. Verdict decides capture policy; this file has none.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_FIELDS = (
    "session_id",
    "turn_id",
    "user_message",
    "assistant_response",
    "is_first_turn",
    "model",
    "platform",
    "sender_id",
    "completed",
    "interrupted",
)
_MAX_FIELD_CHARS = 20_000


def _settings() -> tuple[list[str], float]:
    cfg = json.loads((_HERE / "config.json").read_text(encoding="utf-8"))
    return [str(part) for part in cfg["command"]], float(cfg.get("timeout_s", 8))


def _call(hook: str, kwargs: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key in _FIELDS:
        value = kwargs.get(key)
        if value is None:
            continue
        payload[key] = value[:_MAX_FIELD_CHARS] if isinstance(value, str) else value
    payload["cwd"] = os.getcwd()
    try:
        command, timeout_s = _settings()
        proc = subprocess.run(
            [*command, "hook", hook],
            input=json.dumps(payload, default=str),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
        result = json.loads(proc.stdout or "{}")
        return result if isinstance(result, dict) else {}
    except Exception:
        return {}


def _on_session_start(**kwargs: Any) -> None:
    _call("on_session_start", kwargs)


def _pre_llm_call(**kwargs: Any) -> dict[str, str] | None:
    context = _call("pre_llm_call", kwargs).get("context")
    return {"context": context} if isinstance(context, str) and context else None


def _post_llm_call(**kwargs: Any) -> None:
    _call("post_llm_call", kwargs)


def _on_session_end(**kwargs: Any) -> None:
    _call("on_session_end", kwargs)


HOOKS = {
    "on_session_start": _on_session_start,
    "pre_llm_call": _pre_llm_call,
    "post_llm_call": _post_llm_call,
    "on_session_end": _on_session_end,
}


def register(ctx: Any) -> None:
    for name, callback in HOOKS.items():
        ctx.register_hook(name, callback)
