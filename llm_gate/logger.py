"""JSONL decision logging for cost analysis and ML training."""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from llm_gate.models import RoutingDecision


def log_decision(
    path: Path | str,
    task: str,
    req_tier: int,
    decision: RoutingDecision,
    log_full_task: bool = False,
) -> None:
    """Write a decision record to the JSONL log."""
    try:
        log_file = Path(path)
        log_file.parent.mkdir(parents=True, exist_ok=True)

        task_hash = hashlib.sha256(task.encode()).hexdigest()[:12]
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "task_hash": task_hash,
            "task_preview": task[:120] + "..." if len(task) > 120 else task,
            "task_len": len(task),
            "input_tier": req_tier,
            "effective_tier": decision.tier,
            "escalated": decision.escalated,
            "escalation_reason": decision.escalation_reason,
            "model_chosen": decision.model,
            "provider": decision.provider,
            "alternatives_considered": decision.alternatives,
            "headroom_pct": decision.headroom_pct,
            "latency_ms": decision.latency_ms,
            "reason": decision.reason,
        }
        if log_full_task:
            record["task_full"] = task

        with log_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass  # Never disrupt routing due to a logging failure
