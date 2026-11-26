"""Doctor row ``github_delivery``: gh login can push to and open PRs on the repository.

``gh`` and ``git`` are reached only through a stub runner; no process or network is used.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from verdict import doctor_diagnostics as dd
from verdict.doctor_presentation import repair_command
from verdict.orchestration.deliver_publish import CommandResult

REMOTE = "https://github.com/acme/widgets.git\n"
TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"


class StubGh:
    """Records argv and answers per command; unknown commands fail loudly."""

    def __init__(
        self,
        *,
        version: CommandResult | None = None,
        remote: CommandResult | None = None,
        auth: CommandResult | None = None,
        view: CommandResult | None = None,
    ) -> None:
        self.version = version or CommandResult(0, "gh version 2.50.0\n")
        self.remote = remote or CommandResult(0, REMOTE)
        self.auth = auth or CommandResult(0, "", "Logged in to github.com\n")
        self.view = view or CommandResult(0, "WRITE\n")
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str], cwd: Path | None = None) -> CommandResult:
        command = list(argv)
        self.calls.append(command)
        if command[:2] == ["gh", "--version"]:
            return self.version
        if command[:3] == ["git", "remote", "get-url"]:
            return self.remote
        if command[:3] == ["gh", "auth", "status"]:
            return self.auth
        if command[:3] == ["gh", "repo", "view"]:
            return self.view
        raise AssertionError(f"unexpected command: {command}")


def _text(row: dict[str, str]) -> str:
    return json.dumps(row)


def test_gh_not_installed() -> None:
    stub = StubGh(version=CommandResult(127, "", "[Errno 2] No such file: 'gh'"))
    row = dd.github_delivery_row(stub)
    assert row["label"] == "github_delivery"
    assert row["status"] == "warn"
    assert "not installed" in row["detail"]
    assert "gh auth login" in row["detail"]
    assert stub.calls == [["gh", "--version"]]


def test_gh_not_logged_in() -> None:
    stub = StubGh(auth=CommandResult(1, "", "You are not logged into any GitHub hosts."))
    row = dd.github_delivery_row(stub)
    assert row["status"] == "warn"
    assert "not logged in" in row["detail"]
    assert "gh auth login" in row["detail"]
    assert not any(call[:3] == ["gh", "repo", "view"] for call in stub.calls)


@pytest.mark.parametrize("permission", ["READ", "TRIAGE", "NONE"])
def test_logged_in_without_push_permission(permission: str) -> None:
    row = dd.github_delivery_row(StubGh(view=CommandResult(0, permission + "\n")))
    assert row["status"] == "warn"
    assert "cannot push" in row["detail"]
    assert "acme/widgets" in row["detail"]
    assert "write access" in row["detail"]


@pytest.mark.parametrize("permission", ["WRITE", "MAINTAIN", "ADMIN"])
def test_logged_in_with_push_and_pull_request_permission(permission: str) -> None:
    stub = StubGh(view=CommandResult(0, permission + "\n"))
    row = dd.github_delivery_row(stub)
    assert row["status"] == "ok"
    assert "push to and open pull requests on acme/widgets" in row["detail"]
    view = next(call for call in stub.calls if call[:3] == ["gh", "repo", "view"])
    assert view[3] == "acme/widgets"


def test_unreadable_permission_and_failed_lookup_are_warnings() -> None:
    odd = dd.github_delivery_row(StubGh(view=CommandResult(0, "SOMETHING_NEW\n")))
    failed = dd.github_delivery_row(StubGh(view=CommandResult(1, "", "HTTP 404")))
    timed_out = dd.github_delivery_row(StubGh(view=CommandResult(124, "", "timed out")))
    assert odd["status"] == failed["status"] == timed_out["status"] == "warn"
    assert "timed out" in timed_out["detail"]
    assert "SOMETHING_NEW" not in odd["detail"]


def test_no_github_remote_is_a_note() -> None:
    row = dd.github_delivery_row(StubGh(remote=CommandResult(1, "", "no such remote")))
    assert row["status"] == "note"


def test_output_with_token_never_reaches_the_row() -> None:
    noisy = f"Logged in as octocat (token: {TOKEN})\nToken scopes: 'repo'\n"
    rows = [
        dd.github_delivery_row(
            StubGh(
                version=CommandResult(0, f"gh version 2.50.0 {TOKEN}\n", TOKEN),
                remote=CommandResult(0, f"https://octocat:{TOKEN}@github.com/acme/widgets.git\n"),
                auth=CommandResult(0, noisy, noisy),
                view=CommandResult(0, "WRITE\n", TOKEN),
            )
        ),
        dd.github_delivery_row(StubGh(auth=CommandResult(1, noisy, noisy))),
        dd.github_delivery_row(StubGh(version=CommandResult(127, TOKEN, TOKEN))),
        dd.github_delivery_row(StubGh(view=CommandResult(1, TOKEN, noisy))),
        dd.github_delivery_row(StubGh(view=CommandResult(0, TOKEN + "\n", noisy))),
    ]
    for row in rows:
        assert TOKEN not in _text(row)
        assert "A1b2C3d4" not in _text(row)
        assert "octocat" not in _text(row)


def test_repair_command_for_a_problem_row_is_gh_login() -> None:
    row = dd.github_delivery_row(StubGh(auth=CommandResult(1)))
    assert repair_command(f"{row['label']} {row['status']} {row['detail']}") == "gh auth login"


def test_full_doctor_collection_includes_github_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("VERDICT_HOME", str(home / ".verdict"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("OMNIROUTE_BASE_URL", "http://127.0.0.1:1")
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)

    import verdict.documentation_preflight as documentation_preflight

    monkeypatch.setattr(documentation_preflight, "discover_sources", lambda _root=None: ())
    monkeypatch.setattr(dd, "_omniroute_api_request", lambda *a, **k: None)
    monkeypatch.setattr(dd, "gateway_health_failure", lambda *a, **k: None)
    monkeypatch.setattr(dd, "host_prerequisite_rows", lambda: [])
    monkeypatch.setattr(dd, "_systemctl_show_props", lambda unit: {})
    stub = StubGh()
    monkeypatch.setattr(dd, "_run_gh_command", stub)

    diag = dd._collect_doctor_diagnostics(False, interactive=False)

    labels = [label for label, _state, _detail in diag.sections]
    assert labels.count("github_delivery") == 1
    assert "Capability coverage" in labels
    assert "Documentation preflight" in labels
    assert "Credentials" in labels
    assert "Host prerequisites" in labels
    assert labels.index("github_delivery") > labels.index("Host prerequisites")
    [(_, state, detail)] = [s for s in diag.sections if s[0] == "github_delivery"]
    assert state == "ok"
    assert "acme/widgets" in detail
    assert not any("github_delivery" in item for item in (*diag.issues, *diag.warnings))
    assert stub.calls
