from __future__ import annotations

import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from verdict.patch_executor import (
    PatchExecutor,
    PatchExecutorConfig,
    PatchExecutorError,
    RouteObservation,
    build_unit_prompt,
    extract_diff,
    parse_patch_paths,
)
from verdict.work_unit import WorkUnit

IN_BOUNDS_DIFF = """diff --git a/owned.py b/owned.py
--- a/owned.py
+++ b/owned.py
@@ -1 +1 @@
-old
+new
"""

OUT_OF_BOUNDS_DIFF = """diff --git a/secrets.env b/secrets.env
--- a/secrets.env
+++ b/secrets.env
@@ -1 +1 @@
-old
+new
"""

ESCAPING_DIFF = """--- a/../../etc/passwd
+++ b/../../etc/passwd
@@ -1 +1 @@
-old
+new
"""


def _unit(owned: tuple[str, ...] = ("owned.py",)) -> WorkUnit:
    return WorkUnit(
        unit_id="unit-1",
        objective="replace old with new",
        owned_files=owned,
        verification_command=("true",),
    )


def _response(
    content: str,
    *,
    usage: dict[str, int] | None = None,
    status: int = 200,
    model: str | None = None,
) -> Any:
    body: dict[str, Any] = {"choices": [{"message": {"content": content}}]}
    if usage is not None:
        body["usage"] = usage
    if model is not None:
        body["model"] = model
    return {"status_code": status, "body": body}


class RecordingRunner:
    """Stands in for subprocess.run so we can assert git apply was never invoked."""

    def __init__(self, returncode: int = 0, stderr: str = "") -> None:
        self.calls: list[list[str]] = []
        self.returncode = returncode
        self.stderr = stderr

    def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(args))
        return subprocess.CompletedProcess(args, self.returncode, "", self.stderr)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    (tmp_path / "owned.py").write_text("old\n", encoding="utf-8")
    (tmp_path / "secrets.env").write_text("old\n", encoding="utf-8")
    return tmp_path


def _executor(
    repo: Path, content: str, runner: RecordingRunner, **kwargs: Any
) -> tuple[PatchExecutor, list[Mapping[str, Any]]]:
    seen: list[Mapping[str, Any]] = []

    def transport(model_id: str, payload: Mapping[str, Any], timeout_seconds: float) -> Any:
        seen.append(payload)
        return _response(content, **kwargs)

    executor = PatchExecutor(
        repo,
        PatchExecutorConfig(model="cheap/model", api_key=None),
        transport=transport,
        runner=runner,
    )
    return executor, seen


def test_in_bounds_patch_is_checked_then_applied(repo: Path) -> None:
    runner = RecordingRunner()
    executor, _ = _executor(
        repo, IN_BOUNDS_DIFF, runner, usage={"prompt_tokens": 120, "completion_tokens": 30}
    )

    attempt = executor.execute_unit(_unit())

    assert attempt.applied
    assert attempt.changed_files == ("owned.py",)
    assert attempt.usage.reported is True
    assert attempt.usage.prompt_tokens == 120
    assert attempt.usage.total_tokens == 150
    assert [call[:3] for call in runner.calls] == [
        ["git", "apply", "--whitespace=nowarn"],
        ["git", "apply", "--whitespace=nowarn"],
    ]
    assert "--check" in runner.calls[0]
    assert "--check" not in runner.calls[1]


def test_out_of_bounds_patch_is_rejected_before_git_apply(repo: Path) -> None:
    runner = RecordingRunner()
    executor, _ = _executor(repo, OUT_OF_BOUNDS_DIFF, runner)

    attempt = executor.execute_unit(_unit())

    assert attempt.outcome == "rejected"
    assert "secrets.env" in attempt.reason
    assert runner.calls == []  # the tree was never touched
    assert (repo / "secrets.env").read_text(encoding="utf-8") == "old\n"


def test_patch_mixing_owned_and_unowned_files_is_rejected_entirely(repo: Path) -> None:
    runner = RecordingRunner()
    executor, _ = _executor(repo, IN_BOUNDS_DIFF + OUT_OF_BOUNDS_DIFF, runner)

    attempt = executor.execute_unit(_unit())

    assert attempt.outcome == "rejected"
    assert runner.calls == []


def test_escaping_path_in_diff_header_is_rejected(repo: Path) -> None:
    runner = RecordingRunner()
    executor, _ = _executor(repo, ESCAPING_DIFF, runner)

    attempt = executor.execute_unit(_unit())

    assert attempt.outcome == "rejected"
    assert runner.calls == []


def test_prose_response_is_rejected_as_malformed(repo: Path) -> None:
    runner = RecordingRunner()
    executor, _ = _executor(repo, "Sure! I would change line 1 to say new.", runner)

    attempt = executor.execute_unit(_unit())

    assert attempt.outcome == "rejected"
    assert "not a unified diff" in attempt.reason
    assert runner.calls == []


def test_failing_git_apply_check_rejects_without_mutating(repo: Path) -> None:
    runner = RecordingRunner(returncode=1, stderr="error: patch does not apply")
    executor, _ = _executor(repo, IN_BOUNDS_DIFF, runner)

    attempt = executor.execute_unit(_unit())

    assert attempt.outcome == "rejected"
    assert "does not apply" in attempt.reason
    assert len(runner.calls) == 1  # only the --check probe ran
    assert "--check" in runner.calls[0]


def test_missing_usage_is_recorded_as_unreported_not_estimated(repo: Path) -> None:
    runner = RecordingRunner()
    executor, _ = _executor(repo, IN_BOUNDS_DIFF, runner)

    attempt = executor.execute_unit(_unit())

    assert attempt.applied
    assert attempt.usage.reported is False
    assert attempt.usage.total_tokens == 0


def test_transport_failure_is_an_error_outcome_not_a_crash(repo: Path) -> None:
    runner = RecordingRunner()

    def transport(model_id: str, payload: Mapping[str, Any], timeout_seconds: float) -> Any:
        raise TimeoutError("upstream timed out")

    executor = PatchExecutor(
        repo, PatchExecutorConfig(model="cheap/model"), transport=transport, runner=runner
    )

    attempt = executor.execute_unit(_unit())

    assert attempt.outcome == "error"
    assert "TimeoutError" in attempt.reason
    assert runner.calls == []


def test_http_error_status_is_an_error_outcome(repo: Path) -> None:
    runner = RecordingRunner()
    executor, _ = _executor(repo, IN_BOUNDS_DIFF, runner, status=503)

    attempt = executor.execute_unit(_unit())

    assert attempt.outcome == "error"
    assert "503" in attempt.reason


def test_executor_requires_a_git_repository(tmp_path: Path) -> None:
    with pytest.raises(PatchExecutorError, match="not a git repository"):
        PatchExecutor(tmp_path, PatchExecutorConfig(model="cheap/model"))


def test_prompt_names_the_boundary_and_the_verification_command(repo: Path) -> None:
    prompt = build_unit_prompt(_unit(), repo)

    assert "owned.py" in prompt
    assert "true" in prompt
    assert "old" in prompt  # current contents were inlined
    assert "secrets.env" not in prompt  # unowned files are never shown


def test_default_openai_transport_sends_operational_loop_session_header(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, str] = {}

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, limit):
            del limit
            return b'{"choices":[{"message":{"content":"not-a-diff"}}],"model":"served"}'

    def opener(request, timeout):
        del timeout
        captured["session"] = request.get_header("X-session-id")
        captured["url"] = request.full_url
        return Response()

    monkeypatch.setattr("verdict.patch_executor.urllib.request.urlopen", opener)

    executor = PatchExecutor(
        repo, PatchExecutorConfig(model="cheap/model"), runner=RecordingRunner()
    )
    attempt = executor.execute_unit(_unit())

    assert attempt.outcome == "rejected"
    assert captured["url"].endswith("/chat/completions")
    assert captured["session"] == "verdict-operational-loop"


def test_api_key_is_never_placed_in_the_prompt(repo: Path) -> None:
    runner = RecordingRunner()
    seen: list[Mapping[str, Any]] = []

    def transport(model_id: str, payload: Mapping[str, Any], timeout_seconds: float) -> Any:
        seen.append(payload)
        return _response(IN_BOUNDS_DIFF)

    executor = PatchExecutor(
        repo,
        PatchExecutorConfig(model="cheap/model", api_key="sk-should-never-appear"),
        transport=transport,
        runner=runner,
    )
    attempt = executor.execute_unit(_unit())

    assert attempt.applied
    assert "sk-should-never-appear" not in str(seen)
    assert "sk-should-never-appear" not in str(attempt.to_dict())


def test_fenced_diff_is_unwrapped() -> None:
    assert extract_diff(f"```diff\n{IN_BOUNDS_DIFF}```").startswith("diff --git")


def test_observer_records_requested_alias_distinct_from_served_identity(repo: Path) -> None:
    runner = RecordingRunner()
    seen: list[RouteObservation] = []

    def transport(model_id: str, payload: Mapping[str, Any], timeout_seconds: float) -> Any:
        return _response(IN_BOUNDS_DIFF, model="provider/served-v2")

    executor = PatchExecutor(
        repo,
        PatchExecutorConfig(model="cheap/alias"),
        transport=transport,
        runner=runner,
        observer=seen.append,
    )

    attempt = executor.execute_unit(_unit())

    assert attempt.applied
    assert attempt.model == "cheap/alias"
    assert attempt.resolved_model == "provider/served-v2"
    assert len(seen) == 1
    assert seen[0].model == "cheap/alias"
    assert seen[0].resolved_model == "provider/served-v2"
    assert seen[0].outcome == "ok"
    assert seen[0].identity_mismatch is True
    payload = seen[0].to_dict()
    assert payload["model"] != payload["resolved_model"]
    assert payload["identity_mismatch"] is True


def test_observer_records_transport_failure_without_inventing_served_identity(repo: Path) -> None:
    runner = RecordingRunner()
    seen: list[RouteObservation] = []

    def transport(model_id: str, payload: Mapping[str, Any], timeout_seconds: float) -> Any:
        raise TimeoutError("upstream timed out")

    executor = PatchExecutor(
        repo,
        PatchExecutorConfig(model="cheap/alias"),
        transport=transport,
        runner=runner,
        observer=seen.append,
    )

    attempt = executor.execute_unit(_unit())

    assert attempt.outcome == "error"
    assert len(seen) == 1
    assert seen[0].failed is True
    assert seen[0].resolved_model is None
    assert seen[0].identity_mismatch is False
    assert "TimeoutError" in seen[0].reason


def test_parse_patch_paths_ignores_dev_null_and_strips_prefixes() -> None:
    created = "--- /dev/null\n+++ b/new_file.py\n@@ -0,0 +1 @@\n+x\n"
    assert parse_patch_paths(created) == ("new_file.py",)


def test_parse_patch_paths_requires_at_least_one_file() -> None:
    with pytest.raises(PatchExecutorError, match="names no files"):
        parse_patch_paths("@@ -1 +1 @@\n-old\n+new\n")
