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
    extract_content,
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


def test_identity_mismatch_rejects_patch_before_git_apply(repo: Path) -> None:
    runner = RecordingRunner()
    executor, _ = _executor(repo, IN_BOUNDS_DIFF, runner, model="other/model")

    attempt = executor.execute_unit(_unit())

    assert attempt.outcome == "error"
    assert "identity mismatch" in attempt.reason
    assert attempt.resolved_model == "other/model"
    assert runner.calls == []


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

        @property
        def headers(self):
            return {"x-omniroute-model": "cheap/model"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, limit):
            del limit
            return b'{"choices":[{"message":{"content":"not-a-diff"}}],"model":"cheap/model"}'

    def opener(request, timeout):
        del timeout
        captured["session"] = request.get_header("X-session-id")
        captured["no_cache"] = request.get_header("X-omniroute-no-cache")
        captured["cache_control"] = request.get_header("Cache-control")
        captured["url"] = request.full_url
        return Response()

    monkeypatch.setattr("verdict.patch_executor.open_no_redirect", opener)

    executor = PatchExecutor(
        repo, PatchExecutorConfig(model="cheap/model"), runner=RecordingRunner()
    )
    attempt = executor.execute_unit(_unit())

    assert attempt.outcome == "rejected"
    assert captured["url"].endswith("/chat/completions")
    assert captured["session"] == "verdict-operational-loop"
    # Executor calls must be real samples, never a gateway cache replay.
    assert captured["no_cache"] == "true"
    assert captured["cache_control"] == "no-cache"


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


def test_extract_content_joins_list_and_reasoning_parts() -> None:
    listed = extract_content(
        {"choices": [{"message": {"content": [{"type": "text", "text": "hello"}]}}]}
    )
    assert listed == "hello"
    reasoned = extract_content(
        {"choices": [{"message": {"content": [], "reasoning": "think then answer"}}]}
    )
    assert reasoned == "think then answer"
    with pytest.raises(PatchExecutorError, match="no text content"):
        extract_content({"choices": [{"message": {"content": []}}]})


def test_fenced_diff_is_unwrapped() -> None:
    assert extract_diff(f"```diff\n{IN_BOUNDS_DIFF}```").startswith("diff --git")


def test_extract_diff_fenced_plain() -> None:
    fenced = f"```diff\n{IN_BOUNDS_DIFF}```"
    assert extract_diff(fenced).startswith("diff --git")


def test_extract_diff_fenced_with_surrounding_prose() -> None:
    wrapped = f"Here is the patch:\n\n```diff\n{IN_BOUNDS_DIFF}```\n\nThat should apply cleanly."
    assert extract_diff(wrapped).startswith("diff --git")


def test_extract_diff_refuses_multiple_conflicting_diff_blocks() -> None:
    two_blocks = f"```diff\n{IN_BOUNDS_DIFF}```\nsome commentary\n```diff\n{OUT_OF_BOUNDS_DIFF}```"
    with pytest.raises(PatchExecutorError, match="multiple"):
        extract_diff(two_blocks)


def test_extract_diff_refuses_non_diff_fenced_block() -> None:
    not_a_diff = "```text\njust some notes, no diff here\n```"
    with pytest.raises(PatchExecutorError, match="not a unified diff"):
        extract_diff(not_a_diff)


def test_extract_diff_unmatched_trailing_fence_still_applies() -> None:
    """Reproduces the live 'corrupt patch' failure: the model closes a fence
    it never opened. The diff beneath the stray fence must still parse."""
    stray_fence = IN_BOUNDS_DIFF + "```"
    assert extract_diff(stray_fence).startswith("diff --git")


def test_observer_records_requested_alias_distinct_from_served_identity(repo: Path) -> None:
    runner = RecordingRunner()
    seen: list[RouteObservation] = []

    def transport(model_id: str, payload: Mapping[str, Any], timeout_seconds: float) -> Any:
        return _response(IN_BOUNDS_DIFF, model="provider/served-v2")

    executor = PatchExecutor(
        repo,
        PatchExecutorConfig(model="cheap/alias:free"),
        transport=transport,
        runner=runner,
        observer=seen.append,
    )

    attempt = executor.execute_unit(_unit())

    assert attempt.applied
    assert attempt.model == "cheap/alias:free"
    assert attempt.resolved_model == "provider/served-v2"
    assert len(seen) == 1
    assert seen[0].model == "cheap/alias:free"
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


def test_rename_headers_are_part_of_the_patch_boundary() -> None:
    renamed = """diff --git a/secrets.env b/owned.py
similarity index 100%
rename from secrets.env
rename to owned.py
"""

    assert parse_patch_paths(renamed) == ("owned.py", "secrets.env")


def test_quoted_diff_headers_are_decoded_before_boundary_check() -> None:
    quoted = '--- "a/old\\tname.py"\n+++ "b/new\\tname.py"\n'

    assert parse_patch_paths(quoted) == ("new\tname.py", "old\tname.py")


def test_unquoted_diff_header_with_timestamp_keeps_only_the_path() -> None:
    timestamped = "--- a/owned.py\t2026-09-20 00:00:00 +0000\n+++ b/owned.py\n"

    assert parse_patch_paths(timestamped) == ("owned.py",)


def test_owned_symlink_is_never_inlined_into_the_model_prompt(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    secret = tmp_path / "host-secret.txt"
    secret.write_text("must-not-reach-model\n", encoding="utf-8")
    (repo / "owned.py").symlink_to(secret)

    with pytest.raises(PatchExecutorError, match="symlink"):
        build_unit_prompt(_unit(), repo)


@pytest.mark.parametrize("ambiguous", [False, True])
def test_bare_hunk_uses_unique_source_match(tmp_path: Path, ambiguous: bool) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    source = "before\nold\nafter\n"
    (tmp_path / "owned.py").write_text(source * (2 if ambiguous else 1))
    diff = "--- a/owned.py\n+++ b/owned.py\n@@\n before\n-old\n+new\n after\n"
    executor = PatchExecutor(
        tmp_path, PatchExecutorConfig(model="cheap/model"), transport=lambda *args: _response(diff)
    )
    attempt = executor.execute_unit(_unit())
    assert attempt.applied is not ambiguous
    assert (tmp_path / "owned.py").read_text() == (
        source * 2 if ambiguous else "before\nnew\nafter\n"
    )


def test_missing_prefix_context_line_is_repaired_for_existing_file(tmp_path: Path) -> None:
    """Observed nemotron failure: a context line drops its leading space."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "owned.py").write_text("def f():\n    pass\n", encoding="utf-8")
    diff = "--- a/owned.py\n+++ b/owned.py\n@@ -1,2 +1,2 @@\ndef f():\n-    pass\n+    return 1\n"
    executor = PatchExecutor(
        tmp_path, PatchExecutorConfig(model="cheap/model"), transport=lambda *args: _response(diff)
    )
    attempt = executor.execute_unit(_unit())
    assert attempt.applied
    assert (tmp_path / "owned.py").read_text() == "def f():\n    return 1\n"


def test_new_file_diff_with_old_content_markers_is_repaired(tmp_path: Path) -> None:
    """Observed failure: a diff for a file that does not exist yet still carries a
    modify-style header and removal lines, so git refuses it as depending on old
    contents that are not there. The destination does not exist, so the diff is
    rewritten to a clean new-file hunk: dropped removals, /dev/null source."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    diff = (
        "diff --git a/owned.py b/owned.py\nnew file mode 100644\n"
        "--- a/owned.py\n+++ b/owned.py\n@@ -0,0 +1,2 @@\n"
        "def f():\n-    pass\n+    return 1\n"
    )
    executor = PatchExecutor(
        tmp_path, PatchExecutorConfig(model="cheap/model"), transport=lambda *args: _response(diff)
    )
    attempt = executor.execute_unit(_unit())
    assert attempt.applied
    assert (tmp_path / "owned.py").read_text() == "def f():\n    return 1\n"


def test_repaired_missing_prefix_still_rejected_when_content_does_not_match(tmp_path: Path) -> None:
    """Treating a missing-prefix line as context is a guess; a wrong guess is a
    refusal, not a silently mis-applied patch."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "owned.py").write_text("one\ntwo\nthree\n", encoding="utf-8")
    diff = "--- a/owned.py\n+++ b/owned.py\n@@ -1,3 +1,3 @@\nzero\ntwo\n-three\n+four\n"
    executor = PatchExecutor(
        tmp_path, PatchExecutorConfig(model="cheap/model"), transport=lambda *args: _response(diff)
    )
    attempt = executor.execute_unit(_unit())
    assert not attempt.applied
    assert attempt.outcome == "rejected"


def test_parse_patch_paths_ignores_unreadable_prompt_markers_and_banners() -> None:
    """AC 2 regression test from the safe_div raw output (/tmp/v835-2.json).

    A new-file diff (/dev/null -> path) where the model echoes prompt markers
    like '--- path (unreadable: FileNotFoundError) ---' must ignore those markers
    and pass the boundary check.
    """
    raw_diff = (
        "diff --git a/verdict/demo_safe_div.py b/verdict/demo_safe_div.py\n"
        "new file mode 100644\n"
        "index 0000000..e69de29\n"
        "--- /dev/null\n"
        "+++ b/verdict/demo_safe_div.py\n"
        "@@ -0,0 +1,4 @@\n"
        "+def safe_div(a, b):\n"
        "+    if b == 0:\n"
        "+        return None\n"
        "+    return a / b\n"
        "diff --git a/tests/test_demo_safe_div.py b/tests/test_demo_safe_div.py\n"
        "new file mode 100644\n"
        "index 0000000..e69de29\n"
        "--- /dev/null\n"
        "+++ b/tests/test_demo_safe_div.py\n"
        "@@ -0,0 +1,8 @@\n"
        "+import sys\n"
        "+sys.path.insert(0, '.')\n"
        "+from verdict.demo_safe_div import safe_div\n"
        "+\n"
        "+def test_safe_div_normal():\n"
        "+    assert safe_div(10, 2) == 5.0\n"
        "+    assert safe_div(5, 2) == 2.5\n"
        "+\n"
        "+def test_safe_div_zero():\n"
        "+    assert safe_div(5, 0) is None\n"
        "+    assert safe_div(0, 0) is None\n"
        "--- verdict/demo_safe_div.py (unreadable: FileNotFoundError) ---\n\n"
        "--- tests/test_demo_safe_div.py (unreadable: FileNotFoundError) ---\n"
    )
    paths = parse_patch_paths(raw_diff)
    assert paths == ("tests/test_demo_safe_div.py", "verdict/demo_safe_div.py")

    unit = WorkUnit(
        unit_id="implement-safe-div",
        objective="Add safe_div",
        owned_files=("verdict/demo_safe_div.py", "tests/test_demo_safe_div.py"),
        verification_command=("pytest", "tests/test_demo_safe_div.py"),
    )
    assert unit.out_of_bounds(paths) == ()
    outside = parse_patch_paths(raw_diff + "--- outside.py (unreadable: FileNotFoundError) ---\n")
    assert unit.out_of_bounds(outside) == ("outside.py",)


@pytest.mark.parametrize("prefix", ["+", " "])
def test_bare_hunk_for_new_file_is_added(tmp_path: Path, prefix: str) -> None:
    from verdict.patch_executor import repair_bare_hunks

    diff = "--- /dev/null\n+++ b/new.py\n@@\n" + prefix + "value = 1\n"
    repaired = repair_bare_hunks(diff, tmp_path)
    assert repaired == "--- /dev/null\n+++ b/new.py\n@@ -0,0 +1,1 @@\n+value = 1\n"
    result = subprocess.run(
        ["git", "apply", "-"], cwd=tmp_path, input=repaired, text=True, capture_output=True
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "new.py").read_text() == "value = 1\n"


def test_bare_hunk_for_new_file_rejects_removals(tmp_path: Path) -> None:
    from verdict.patch_executor import repair_bare_hunks

    diff = "--- /dev/null\n+++ b/new.py\n@@\n-old\n+new\n"
    with pytest.raises(PatchExecutorError):
        repair_bare_hunks(diff, tmp_path)


def test_truncated_reply_is_rejected_by_name(repo: Path) -> None:
    def transport(model_id: str, payload: Mapping[str, Any], timeout_seconds: float) -> Any:
        return {
            "status_code": 200,
            "body": {
                "model": "m/x",
                "choices": [
                    {
                        "message": {
                            "content": "--- a/owned.py\n+++ b/owned.py\n@@ -1 +1 @@\n-old\n+ne"
                        },
                        "finish_reason": "length",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4096, "total_tokens": 4106},
            },
        }

    runner = RecordingRunner()
    executor = PatchExecutor(
        repo, PatchExecutorConfig(model="m/x"), transport=transport, runner=runner
    )
    attempt = executor.execute_unit(_unit())

    assert attempt.outcome == "rejected"
    assert "truncated" in attempt.reason
    assert "max_tokens=4096" in attempt.reason
    assert attempt.changed_files == ()
    assert runner.calls == []  # git apply never ran on a cut-off diff
    assert (repo / "owned.py").read_text(encoding="utf-8") == "old\n"


BAD_CONTEXT_DIFF = """diff --git a/owned.py b/owned.py
--- a/owned.py
+++ b/owned.py
@@ -1 +1 @@
-not the real line
+new
"""

LARGE_LINES = 301


def _git_repo(tmp_path: Path, text: str = "old\n") -> Path:
    subprocess.run(["git", "init", "-q", "."], cwd=tmp_path, check=True)
    (tmp_path / "owned.py").write_text(text, encoding="utf-8")
    return tmp_path


def _large_text() -> str:
    return "old\n" + "".join(f"line{n}\n" for n in range(2, LARGE_LINES + 1))


def _sequenced_executor(
    repo: Path, replies: list[str]
) -> tuple[PatchExecutor, list[Mapping[str, Any]]]:
    seen: list[Mapping[str, Any]] = []

    def transport(model_id: str, payload: Mapping[str, Any], timeout_seconds: float) -> Any:
        seen.append(payload)
        return _response(
            replies[len(seen) - 1], usage={"prompt_tokens": 10, "completion_tokens": 5}
        )

    executor = PatchExecutor(
        repo, PatchExecutorConfig(model="cheap/model", api_key=None), transport=transport
    )
    return executor, seen


def test_small_file_repair_asks_for_full_content_and_applies(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path, "import os\nimport sys\n\n\nx = 1\n")
    full = "```file:owned.py\nimport sys\n\n\nx = 1\n```"
    executor, seen = _sequenced_executor(repo, [BAD_CONTEXT_DIFF, full])

    attempt = executor.execute_unit(_unit())

    assert attempt.applied
    assert (repo / "owned.py").read_text(encoding="utf-8") == "import sys\n\n\nx = 1\n"
    assert attempt.changed_files == ("owned.py",)
    assert attempt.repair_attempt is not None
    assert attempt.repair_attempt["format"] == "full_file"
    assert attempt.repair_attempt["repaired"] is True
    assert "patch failed" in attempt.repair_attempt["first_error"]
    assert attempt.usage.total_tokens == 30  # both turns are counted
    assert len(seen) == 2
    repair_messages = seen[1]["messages"]
    assert repair_messages[:2] == seen[0]["messages"]
    assert repair_messages[2] == {"role": "assistant", "content": BAD_CONTEXT_DIFF}
    repair_prompt = repair_messages[3]["content"]
    assert attempt.repair_attempt["first_error"] in repair_prompt
    assert "```file:owned.py" in repair_prompt


def test_full_file_repair_keeps_a_missing_final_newline(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path, "old\nkeep")
    full = "```file:owned.py\nnew\nkeep\n```"
    executor, _ = _sequenced_executor(repo, [BAD_CONTEXT_DIFF, full])

    attempt = executor.execute_unit(_unit())

    assert attempt.applied
    assert (repo / "owned.py").read_text(encoding="utf-8") == "new\nkeep"


def test_truncated_full_file_repair_is_rejected_by_name(tmp_path: Path) -> None:
    original = "".join(f"value_{n} = {n}\n" for n in range(20))
    repo = _git_repo(tmp_path, original)
    executor, seen = _sequenced_executor(
        repo, [BAD_CONTEXT_DIFF, "```file:owned.py\nvalue_0 = 0\n```"]
    )

    attempt = executor.execute_unit(_unit())

    assert attempt.outcome == "rejected"
    assert attempt.reason.startswith("patch repair failed: truncated full-file reply for owned.py")
    assert attempt.repair_attempt == {
        "format": "full_file",
        "first_error": attempt.repair_attempt["first_error"],
        "repaired": False,
    }
    assert len(seen) == 2
    assert (repo / "owned.py").read_text(encoding="utf-8") == original


def test_full_file_repair_outside_the_boundary_is_rejected(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path)
    (repo / "secrets.env").write_text("old\n", encoding="utf-8")
    executor, _ = _sequenced_executor(repo, [BAD_CONTEXT_DIFF, "```file:secrets.env\nnew\n```"])

    attempt = executor.execute_unit(_unit())

    assert attempt.outcome == "rejected"
    assert "outside the unit boundary" in attempt.reason
    assert (repo / "secrets.env").read_text(encoding="utf-8") == "old\n"


def test_large_file_keeps_the_diff_repair(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path, _large_text())
    # A hunk with no trailing context only matches at EOF, so the fixture keeps one.
    repaired_diff = (
        "diff --git a/owned.py b/owned.py\n--- a/owned.py\n+++ b/owned.py\n"
        "@@ -1,2 +1,2 @@\n-old\n+new\n line2\n"
    )
    executor, seen = _sequenced_executor(repo, [BAD_CONTEXT_DIFF, repaired_diff])

    attempt = executor.execute_unit(_unit())

    assert attempt.applied
    assert (repo / "owned.py").read_text(encoding="utf-8").startswith("new\nline2\n")
    assert attempt.repair_attempt is not None
    assert attempt.repair_attempt["format"] == "diff"
    assert attempt.repair_attempt["repaired"] is True
    assert "   1| old" in seen[1]["messages"][3]["content"]


def test_failed_repair_is_rejected_by_name_and_tree_is_untouched(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path, _large_text())
    executor, seen = _sequenced_executor(repo, [BAD_CONTEXT_DIFF, BAD_CONTEXT_DIFF])

    attempt = executor.execute_unit(_unit())

    assert attempt.outcome == "rejected"
    assert attempt.reason.startswith("patch repair failed: git apply --check failed")
    assert attempt.repair_attempt is not None
    assert attempt.repair_attempt["repaired"] is False
    assert len(seen) == 2  # exactly one repair turn, never more
    assert (repo / "owned.py").read_text(encoding="utf-8") == _large_text()


def test_non_apply_rejection_gets_no_repair_turn(repo: Path) -> None:
    executor, seen = _sequenced_executor(repo, [OUT_OF_BOUNDS_DIFF, IN_BOUNDS_DIFF])

    attempt = executor.execute_unit(_unit())

    assert attempt.outcome == "rejected"
    assert attempt.repair_attempt is None
    assert len(seen) == 1
