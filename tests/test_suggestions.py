import json

from llm_gate.suggestions import SuggestionService


def test_generate_suggestions(tmp_path):
    log_file = tmp_path / "test_logs.jsonl"

    # Generate some mock latency faults
    lines = []
    for i in range(12):
        lines.append(json.dumps({
            "task_hash": f"abc{i}",
            "latency_ms": 3000,
            "escalated": False,
            "model_chosen": "slow-model"
        }))

    for i in range(6):
        lines.append(json.dumps({
            "task_hash": f"def{i}",
            "latency_ms": 100,
            "escalated": True,
            "effective_tier": 1,
            "escalation_reason": "timeout"
        }))

    for i in range(4):
        lines.append(json.dumps({
            "task_hash": f"ghi{i}",
            "latency_ms": 100,
            "escalated": False,
            "headroom_pct": 0.10
        }))

    log_file.write_text("\n".join(lines))

    svc = SuggestionService(log_path=str(log_file))
    suggestions = svc.generate_suggestions()

    assert len(suggestions) == 3
    categories = [s.category for s in suggestions]
    assert "performance" in categories
    assert "reliability" in categories
    assert "capacity" in categories

    # Assert missing file drops silently
    svc2 = SuggestionService(log_path="nonexistent.jsonl")
    suggestions2 = svc2.generate_suggestions()
    assert len(suggestions2) == 0

def test_no_raw_prompts_leaked(tmp_path):
    log_file = tmp_path / "test_logs.jsonl"
    for i in range(12):
        log_file.open("a").write(json.dumps({
            "task_hash": f"abc{i}",
            "task_preview": "This is a secret prompt!",
            "latency_ms": 3000,
        }) + "\n")

    svc = SuggestionService(log_path=str(log_file))
    suggestions = svc.generate_suggestions()
    assert len(suggestions) > 0
    s = suggestions[0]
    assert "secret" not in s.title.lower()
    assert "secret" not in s.description.lower()
