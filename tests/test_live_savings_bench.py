"""Unit tests for the live savings benchmark script (offline, no credentials)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

TASKS_JSON = (
    Path(__file__).resolve().parent.parent
    / "benchmarks"
    / "fixtures"
    / "live_savings"
    / "tasks.json"
)
FIXTURE_DIR = TASKS_JSON.parent


class TestOptIn:
    """The benchmark must refuse to run without the opt-in env var."""

    def test_refuses_without_opt_in(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.delenv("VERDICT_LIVE_SMOKE", raising=False)
        from scripts.live_savings_bench import _refuse_without_opt_in

        with pytest.raises(SystemExit):
            _refuse_without_opt_in()


class TestTaskFixtures:
    """Validate the tasks.json fixture file itself."""

    def test_tasks_json_loads(self) -> None:
        data = json.loads(TASKS_JSON.read_text(encoding="utf-8"))
        assert "tasks" in data
        assert len(data["tasks"]) >= 10

    def test_each_task_has_required_fields(self) -> None:
        data = json.loads(TASKS_JSON.read_text(encoding="utf-8"))
        for task in data["tasks"]:
            assert "id" in task
            assert "task_class" in task
            assert "prompt" in task
            assert "test_file" in task
            assert "task_group" in task

    def test_each_task_test_file_exists(self) -> None:
        data = json.loads(TASKS_JSON.read_text(encoding="utf-8"))
        for task in data["tasks"]:
            test_path = FIXTURE_DIR / task["test_file"]
            assert test_path.exists(), f"{task['test_file']} missing"

    def test_task_ids_unique(self) -> None:
        data = json.loads(TASKS_JSON.read_text(encoding="utf-8"))
        ids = [t["id"] for t in data["tasks"]]
        assert len(ids) == len(set(ids))

    def test_has_both_task_groups(self) -> None:
        data = json.loads(TASKS_JSON.read_text(encoding="utf-8"))
        groups = {t["task_group"] for t in data["tasks"]}
        assert "standalone" in groups
        assert "repo-context" in groups

    def test_at_least_5_repo_context_tasks(self) -> None:
        data = json.loads(TASKS_JSON.read_text(encoding="utf-8"))
        rc = [t for t in data["tasks"] if t["task_group"] == "repo-context"]
        assert len(rc) >= 5


class TestGrading:
    """Verify the offline grading mechanism works (correct and wrong answers)."""

    def test_fizzbuzz_correct_solution(self, tmp_path: Path) -> None:
        from scripts.live_savings_bench import _grade_solution

        code = (
            "def fizzbuzz(n: int) -> list[str]:\n"
            "    result = []\n"
            "    for i in range(1, n + 1):\n"
            "        if i % 15 == 0:\n"
            "            result.append('FizzBuzz')\n"
            "        elif i % 3 == 0:\n"
            "            result.append('Fizz')\n"
            "        elif i % 5 == 0:\n"
            "            result.append('Buzz')\n"
            "        else:\n"
            "            result.append(str(i))\n"
            "    return result\n"
        )
        task = {"id": "fizzbuzz", "test_file": "test_fizzbuzz.py"}
        assert _grade_solution(task, code) is True

    def test_fizzbuzz_wrong_solution_fails(self, tmp_path: Path) -> None:
        from scripts.live_savings_bench import _grade_solution

        code = "def fizzbuzz(n: int) -> list[str]:\n    return [str(i) for i in range(1, n + 1)]\n"
        task = {"id": "fizzbuzz", "test_file": "test_fizzbuzz.py"}
        assert _grade_solution(task, code) is False

    def test_binary_search_correct(self, tmp_path: Path) -> None:
        from scripts.live_savings_bench import _grade_solution

        code = (
            "def binary_search(arr: list[int], target: int) -> int:\n"
            "    lo, hi = 0, len(arr) - 1\n"
            "    while lo <= hi:\n"
            "        mid = (lo + hi) // 2\n"
            "        if arr[mid] == target:\n"
            "            return mid\n"
            "        elif arr[mid] < target:\n"
            "            lo = mid + 1\n"
            "        else:\n"
            "            hi = mid - 1\n"
            "    return -1\n"
        )
        task = {"id": "binary_search", "test_file": "test_binary_search.py"}
        assert _grade_solution(task, code) is True


class TestPriceFetching:
    """Test that price fetching logic works structurally."""

    def test_model_price_keys_cover_all_models(self) -> None:
        from scripts.live_savings_bench import BASELINE_MODEL, MODEL_PRICE_KEYS, VERDICT_CANDIDATES

        assert BASELINE_MODEL in MODEL_PRICE_KEYS
        for m in VERDICT_CANDIDATES:
            assert m in MODEL_PRICE_KEYS


class TestCodeExtraction:
    """Verify code extraction from model responses."""

    def test_markdown_fence(self) -> None:
        from scripts.live_savings_bench import _extract_code

        text = "Here:\n```python\ndef f(): pass\n```\nDone."
        assert _extract_code(text).strip() == "def f(): pass"

    def test_raw_code_passthrough(self) -> None:
        from scripts.live_savings_bench import _extract_code

        text = "def f(): pass"
        assert _extract_code(text) == text


class TestPerPairEligibility:
    """Verify that build_report uses per-(task, repeat) eligibility."""

    def test_failed_repeat_excluded(self) -> None:
        from scripts.live_savings_bench import TaskResult, TokenCounter, build_report

        tc = {
            "cc/claude-opus-5": TokenCounter(model="cc/claude-opus-5", proportional=True),
            "cc/claude-haiku-4-5-20251001": TokenCounter(
                model="cc/claude-haiku-4-5-20251001", proportional=True
            ),
        }
        price_table = {
            "cc/claude-opus-5": {"input_per_1m": 5.0, "output_per_1m": 25.0},
            "cc/claude-haiku-4-5-20251001": {"input_per_1m": 1.0, "output_per_1m": 5.0},
        }
        results = [
            TaskResult("t1", "baseline", "cc/claude-opus-5", 1, True, 100, 50, 150, 100),
            TaskResult("t1", "verdict", "cc/claude-haiku-4-5-20251001", 1, True, 100, 50, 150, 80),
            TaskResult("t1", "baseline", "cc/claude-opus-5", 2, False, 100, 50, 150, 100),
            TaskResult("t1", "verdict", "cc/claude-haiku-4-5-20251001", 2, True, 100, 50, 150, 80),
        ]
        tasks = [{"id": "t1", "task_group": "standalone"}]
        report = build_report(results, tc, tasks, price_table, "sha256abc", "2026-09-28T00:00:00Z")

        # Only r1 should be eligible (r2 baseline failed)
        assert len(report["summary"]["cost_eligible_pairs"]) == 1
        assert report["summary"]["cost_eligible_pairs"][0] == "t1/r1"
        assert len(report["summary"]["cost_excluded_pairs"]) == 1
        assert report["summary"]["cost_excluded_pairs"][0]["pair"] == "t1/r2"
        assert "baseline failed" in report["summary"]["cost_excluded_pairs"][0]["reason"]
