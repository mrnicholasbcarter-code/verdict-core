import subprocess
import sys

from verdict.benchmarking import _stable_task_seed


def test_task_seed_is_stable_across_processes():
    expected = _stable_task_seed("same task", 17)
    code = "from verdict.benchmarking import _stable_task_seed; print(_stable_task_seed('same task', 17))"
    observed = subprocess.check_output([sys.executable, "-c", code], text=True).strip()
    assert int(observed) == expected
