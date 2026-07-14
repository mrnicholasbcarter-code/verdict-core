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
            "event_version": decision.event_version,
            "policy_version": decision.policy_version,
            "request_id": decision.request_id,
            "task_hash": task_hash,
            "task_preview": f"[redacted:{task_hash} len={len(task)}]",
            "task_len": len(task),
            "input_tier": req_tier,
            "task_class": decision.task_class,
            "protected": decision.protected,
            "degraded_mode": decision.degraded_mode,
            "managed_backend_status": decision.managed_backend_status,
            "decision": decision.decision,
            "effective_tier": decision.tier,
            "escalated": decision.escalated,
            "escalation_reason": decision.escalation_reason,
            "model_chosen": decision.model,
            "provider": decision.provider,
            "alternatives_considered": decision.alternatives,
            "candidate_states": decision.candidate_states,
            "safety_flags": decision.safety_flags,
            "headroom_pct": decision.headroom_pct,
            "latency_ms": decision.latency_ms,
            "reason": decision.reason,
            "transport_outcome": decision.transport_outcome,
            "quality_outcome": decision.quality_outcome,
            "quality_score": decision.quality_score,
        }

        if log_full_task:
            record["task_full"] = task

        with log_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass  # Never disrupt routing due to a logging failure
