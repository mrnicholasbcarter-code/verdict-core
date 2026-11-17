"""Capability-restricted GitHub client for the PR watcher.

Reads PRs, checks, reviews, and merges.  Bounded reruns only.
No merge, push, branch creation, or source-edit methods are exposed.
"""

from __future__ import annotations

import subprocess
from typing import Any, Protocol


class WatcherGitHubClient(Protocol):
    """Read-only plus bounded rerun GitHub client.

    Deliberately excludes mutation methods.

    A PR dict may carry ``linear_issue``, the id of the Linear story the PR is
    linked to.  The watcher comments on that story and on nothing else; a PR
    without the field gets a digest event only.
    """

    def list_open_prs(self, repo: str) -> list[dict[str, Any]]: ...
    def get_pr(self, repo: str, pr_number: int) -> dict[str, Any]: ...
    def list_merged_prs(self, repo: str, since: str) -> list[dict[str, Any]]: ...
    def rerun_check(self, job_id: int | str, repo: str) -> dict[str, Any]: ...


# --- Capability verification -----------------------------------------------------

_MUTATION_METHODS = frozenset(
    [
        "merge",
        "push",
        "create_branch",
        "delete_branch",
        "edit_source",
        "force_push",
        "checkout",
        "commit",
        "apply_patch",
        "write_file",
        "create_pull_request",
        "delete_repository",
    ]
)


def assert_no_mutation(client: WatcherGitHubClient) -> None:
    """Raise TypeError if the client exposes any forbidden mutation method."""
    methods = {m for m in dir(client) if not m.startswith("_")}
    bad = methods & _MUTATION_METHODS
    if bad:
        raise TypeError(f"Watcher client must not expose mutation methods: {bad}")


# --- Concrete gh-api client -------------------------------------------------------


class _GhApiClient:
    """Real client that shelled out to ``gh api``.

    This class is **not** part of the public API.  Tests should inject fakes
    that satisfy ``WatcherGitHubClient`` instead.
    """

    def _call(self, *args: str) -> str:
        out = subprocess.run(
            ["gh", "api", *args], capture_output=True, text=True, check=True, timeout=30
        )
        return out.stdout

    def list_open_prs(self, repo: str) -> list[dict[str, Any]]:
        import json

        raw = self._call(f"repos/{repo}/pulls", "-s", "open")
        if not raw.strip():
            return []
        prs: list[dict[str, Any]] = json.loads(raw)
        result: list[dict[str, Any]] = []
        for pr in prs:
            result.append(
                {
                    "number": pr["number"],
                    "head_sha": pr["head"]["sha"],
                    "mergeable": pr.get("mergeable"),
                    "merge_state_status": pr.get("mergeable_state", ""),
                    "state": pr["state"],
                }
            )
        return result

    def get_pr(self, repo: str, pr_number: int) -> dict[str, Any]:
        import json

        data: dict[str, Any] = json.loads(self._call(f"repos/{repo}/pulls/{pr_number}"))
        return {
            "number": pr_number,
            "head_sha": data["head"]["sha"],
            "mergeable": data.get("mergeable"),
            "merge_state_status": data.get("mergeable_state", ""),
            "state": data["state"],
            "checks": self._checks(repo, data["head"]["sha"]),
        }

    def _checks(self, repo: str, sha: str) -> list[dict[str, Any]]:
        import json

        data: dict[str, Any] = json.loads(self._call(f"repos/{repo}/commits/{sha}/check-runs"))
        result: list[dict[str, Any]] = []
        for cr in data.get("check_runs", []):
            result.append(
                {
                    "name": cr["name"],
                    "status": cr["status"],
                    "conclusion": cr.get("conclusion"),
                    "head_sha": cr.get("head_sha", sha),
                    "job_id": cr["id"],
                }
            )
        return result

    def _reviews(self, repo: str, pr_number: int) -> list[dict[str, Any]]:
        import json

        data: list[dict[str, Any]] = json.loads(
            self._call(f"repos/{repo}/pulls/{pr_number}/reviews")
        )
        return [{"state": r["state"], "id": r["id"]} for r in data]

    def list_merged_prs(self, repo: str, since: str) -> list[dict[str, Any]]:
        import json

        q = f"repo:{repo} is:pr is:merged"
        if since:
            q += f" closed:>={since}"
        data: dict[str, Any] = json.loads(self._call("search/issues", "-F", f"q={q}"))
        result: list[dict[str, Any]] = []
        for item in data.get("items", []):
            result.append(
                {
                    "number": item["number"],
                    "head_sha": item["head"]["sha"],
                    "merged_at": item.get("closed_at", ""),
                    "state": "closed",
                    "reviews": self._reviews(repo, item["number"]),
                }
            )
        return result

    def rerun_check(self, job_id: int | str, repo: str) -> dict[str, Any]:
        import json

        data = json.loads(self._call(f"repos/{repo}/check-runs/{job_id}/rerun"))
        assert isinstance(data, dict)
        return data


def create_github_client() -> WatcherGitHubClient:
    """Create a real gh-api backed client.

    Tests should inject fakes instead.
    """
    return _GhApiClient()
