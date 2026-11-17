"""Capability-restricted GitHub client for the PR watcher.

Reads go through ``gh api graphql``; the single write is the REST rerun of one
GitHub Actions job.  No merge, push, branch creation, or source-edit method is
exposed.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Sequence
from typing import Any, Protocol


class GitHubClientError(Exception):
    """A GitHub read or write failed; the watcher skips that repository this run."""


class WatcherGitHubClient(Protocol):
    """Read-only plus bounded rerun GitHub client.

    Deliberately excludes mutation methods.  Every method raises
    ``GitHubClientError`` when GitHub cannot be read or the answer cannot be
    trusted.

    ``list_open_prs`` returns ``number``, ``head_sha``, ``mergeable`` and
    ``merge_state_status`` per open PR.  ``get_pr`` returns the same for one PR
    plus ``commit_oid`` (the commit its checks belong to), ``linear_issue`` and
    ``checks``; each check has ``name``, ``status``, ``conclusion``,
    ``required``, ``job_id``, ``workflow_run_id``, ``details_url`` and
    ``started_at``.  ``job_id`` is set only for a GitHub Actions job, the one
    kind of check ``rerun_check`` can rerun.  ``list_merged_prs`` returns
    ``number``, ``head_sha``, ``merged_at``, ``linear_issue`` and ``reviews``
    (``state``, ``submitted_at``, ``commit_oid``) per PR merged since ``since``.
    """

    def list_open_prs(self, repo: str) -> list[dict[str, Any]]: ...
    def get_pr(self, repo: str, pr_number: int) -> dict[str, Any]: ...
    def list_merged_prs(self, repo: str, since: str) -> list[dict[str, Any]]: ...
    def rerun_check(self, job_id: int | str, repo: str) -> dict[str, Any]: ...


# --- Capability verification -----------------------------------------------------

_ALLOWED_METHODS = frozenset({"list_open_prs", "get_pr", "list_merged_prs", "rerun_check"})


def assert_no_mutation(client: WatcherGitHubClient) -> None:
    """Raise TypeError if the client exposes any forbidden mutation method."""
    methods = {m for m in dir(client) if not m.startswith("_") and callable(getattr(client, m))}
    helper_methods = {m for m in methods if m.startswith(("set_", "fail_"))}
    bad = methods - _ALLOWED_METHODS - helper_methods
    if bad:
        raise TypeError(f"Watcher client exposes unsupported methods: {bad}")


# --- Linear story link -----------------------------------------------------------

# A branch names its story at the start of a path segment (``chore/bod-195-fix``).
# Anywhere else a version or a date reads the same way (``otlp-1``, ``agent-17``).
_BRANCH_ISSUE = re.compile(r"(?:^|/)([A-Za-z]{2,10}-\d+)(?=$|[-_/])")
# In prose an identifier is upper case and stands alone; ``CVE-2026-1234`` and
# ``v1.BOD-2`` are not identifiers.
_BODY_ISSUE = re.compile(r"(?<![A-Za-z0-9.-])([A-Z]{2,10}-\d+)(?![A-Za-z0-9-])")


def linear_issue_for(branch: str, body: str, team_keys: Sequence[str] | None = None) -> str | None:
    """Linear issue a PR is linked to: from its branch name, else from its body.

    The pattern alone cannot tell ``SHA-256`` or ``RFC-2616`` in a PR body from
    an issue.  ``team_keys`` (for example ``("BOD",)``) restricts the result to
    those Linear teams; without it any identifier-shaped token is taken.
    """
    allowed = None if team_keys is None else {key.upper() for key in team_keys}
    candidates: list[str] = [m.upper() for m in _BRANCH_ISSUE.findall(branch)]
    candidates += _BODY_ISSUE.findall(body)
    for candidate in candidates:
        if allowed is None or candidate.split("-", 1)[0] in allowed:
            return candidate
    return None


# --- Queries ---------------------------------------------------------------------

OPEN_PULLS_PAGE_SIZE = 50
CONTEXTS_PAGE_SIZE = 100
MERGED_PULLS_PAGE_SIZE = 50
# Hard cap on pages per listing. A listing that needs more is refused, not
# truncated: 1000 open PRs, 2000 checks on one PR, 1000 merged PRs per window.
MAX_PAGES = 20
CALL_TIMEOUT_SECONDS = 30

OPEN_PULLS_QUERY = """
query($owner: String!, $name: String!, $first: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: OPEN, first: $first, after: $after) {
      pageInfo { hasNextPage endCursor }
      nodes { number mergeable mergeStateStatus headRefOid }
    }
  }
}
""".strip()

# The PR head, the commit the checks belong to, and the checks come back in one
# node, so they cannot drift apart within a page.
PULL_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $first: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      number mergeable mergeStateStatus headRefOid headRefName body
      commits(last: 1) {
        nodes {
          commit {
            oid
            statusCheckRollup {
              state
              contexts(first: $first, after: $after) {
                totalCount
                pageInfo { hasNextPage endCursor }
                nodes {
                  __typename
                  ... on CheckRun {
                    databaseId name status conclusion startedAt completedAt detailsUrl
                    isRequired(pullRequestNumber: $number)
                    checkSuite { workflowRun { databaseId workflow { name } } }
                  }
                  ... on StatusContext {
                    context state
                    isRequired(pullRequestNumber: $number)
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}
""".strip()

MERGED_PULLS_QUERY = """
query($q: String!, $first: Int!, $after: String) {
  search(query: $q, type: ISSUE, first: $first, after: $after) {
    issueCount
    pageInfo { hasNextPage endCursor }
    nodes {
      ... on PullRequest {
        number mergedAt headRefOid headRefName body
        reviews(last: 50) { nodes { state submittedAt commit { oid } } }
      }
    }
  }
}
""".strip()

_REPO_PART = re.compile(r"[A-Za-z0-9_.-]+")
_SINCE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
# Commit-status states, mapped onto the check-run vocabulary the policy reads.
_STATUS_STATES: dict[str, tuple[str, str | None]] = {
    "SUCCESS": ("COMPLETED", "SUCCESS"),
    "FAILURE": ("COMPLETED", "FAILURE"),
    "ERROR": ("COMPLETED", "FAILURE"),
}


def _split_repo(repo: str) -> tuple[str, str]:
    owner, _, name = repo.partition("/")
    for part in (owner, name):
        if not _REPO_PART.fullmatch(part) or part in {".", ".."}:
            raise GitHubClientError(f"invalid repository {repo!r}: expected owner/name")
    return owner, name


def _check(node: dict[str, Any]) -> dict[str, Any]:
    """One rollup context as the check dict the core consumes."""
    if node["__typename"] == "StatusContext":
        status, conclusion = _STATUS_STATES.get(node["state"], ("IN_PROGRESS", None))
        return {
            "name": node["context"],
            "status": status,
            "conclusion": conclusion,
            "required": node["isRequired"] is True,
            "job_id": None,
            "workflow_run_id": None,
            "details_url": None,
            "started_at": None,
        }
    # Only a check run that belongs to a workflow run is an Actions job, and
    # only those can be rerun through the jobs endpoint.
    workflow_run = (node.get("checkSuite") or {}).get("workflowRun")
    return {
        "name": node["name"],
        "status": node["status"],
        "conclusion": node["conclusion"],
        "required": node["isRequired"] is True,
        "job_id": node["databaseId"] if workflow_run else None,
        "workflow_run_id": workflow_run["databaseId"] if workflow_run else None,
        "details_url": node.get("detailsUrl"),
        "started_at": node.get("startedAt"),
    }


# --- Concrete gh client -----------------------------------------------------------


class _GhApiClient:
    """Real client that shells out to ``gh api``.

    This class is **not** part of the public API.  Tests replace ``_call`` with
    payload fixtures; nothing else is faked.
    """

    def __init__(self, linear_team_keys: Sequence[str] | None = None) -> None:
        self._linear_team_keys = linear_team_keys

    def _call(self, *args: str) -> str:
        try:
            done = subprocess.run(
                ["gh", "api", *args],
                capture_output=True,
                text=True,
                check=True,
                timeout=CALL_TIMEOUT_SECONDS,
            )
        except subprocess.CalledProcessError as exc:
            lines = (exc.stderr or "").strip().splitlines()
            detail = lines[0][:300] if lines else f"exit {exc.returncode}"
            raise GitHubClientError(f"gh api failed: {detail}") from exc
        except subprocess.TimeoutExpired as exc:
            raise GitHubClientError(f"gh api timed out after {CALL_TIMEOUT_SECONDS}s") from exc
        except OSError as exc:
            raise GitHubClientError(f"gh could not be run: {exc}") from exc
        return done.stdout

    def _graphql(self, query: str, variables: dict[str, str | int | None]) -> dict[str, Any]:
        """Run one read query; ``-F`` types the integers, ``-f`` keeps strings raw."""
        argv = ["graphql", "-f", f"query={query}"]
        for name, value in variables.items():
            if value is not None:
                argv += ["-F" if isinstance(value, int) else "-f", f"{name}={value}"]
        raw = self._call(*argv)
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            raise GitHubClientError("GitHub returned a response that is not JSON") from exc
        if not isinstance(payload, dict):
            raise GitHubClientError("GitHub returned a response that is not a JSON object")
        if payload.get("errors"):
            first = payload["errors"][0]
            message = first.get("message") if isinstance(first, dict) else first
            raise GitHubClientError(f"GitHub GraphQL error: {str(message)[:300]}")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise GitHubClientError("GitHub returned a response with no data")
        return data

    def _pages(
        self,
        what: str,
        fetch: Callable[[str | None], dict[str, Any]],
        connection: Callable[[dict[str, Any]], dict[str, Any] | None],
        visit: Callable[[dict[str, Any], dict[str, Any]], None],
    ) -> None:
        """Walk a paginated connection, at most ``MAX_PAGES`` pages."""
        after: str | None = None
        for _ in range(MAX_PAGES):
            data = fetch(after)
            try:
                page = connection(data)
                if page is None:
                    return
                visit(data, page)
                info = page["pageInfo"]
                more, after = info["hasNextPage"], info["endCursor"]
            except (KeyError, TypeError, IndexError, AttributeError) as exc:
                raise GitHubClientError(
                    f"{what}: unexpected GitHub response shape ({type(exc).__name__}: {exc})"
                ) from exc
            if not more:
                return
        raise GitHubClientError(f"{what}: more than {MAX_PAGES} pages")

    def list_open_prs(self, repo: str) -> list[dict[str, Any]]:
        owner, name = _split_repo(repo)
        prs: list[dict[str, Any]] = []

        def visit(_: dict[str, Any], page: dict[str, Any]) -> None:
            prs.extend(
                {
                    "number": node["number"],
                    "head_sha": node["headRefOid"],
                    "mergeable": node["mergeable"],
                    "merge_state_status": node["mergeStateStatus"],
                }
                for node in page["nodes"]
            )

        self._pages(
            f"open pull requests of {repo}",
            lambda after: self._graphql(
                OPEN_PULLS_QUERY,
                {"owner": owner, "name": name, "first": OPEN_PULLS_PAGE_SIZE, "after": after},
            ),
            lambda data: data["repository"]["pullRequests"],
            visit,
        )
        return prs

    def get_pr(self, repo: str, pr_number: int) -> dict[str, Any]:
        owner, name = _split_repo(repo)
        seen: dict[str, Any] = {}
        checks: list[dict[str, Any]] = []

        def commit_of(data: dict[str, Any]) -> dict[str, Any]:
            commit: dict[str, Any] = data["repository"]["pullRequest"]["commits"]["nodes"][0][
                "commit"
            ]
            return commit

        def connection(data: dict[str, Any]) -> dict[str, Any] | None:
            pull = data["repository"]["pullRequest"]
            commit = commit_of(data)
            if not seen:
                seen.update(pull=pull, head=pull["headRefOid"], oid=commit["oid"], moved=False)
            elif (pull["headRefOid"], commit["oid"]) != (seen["head"], seen["oid"]):
                # A push landed between two pages: the pages describe different
                # commits, so no check list can be trusted.
                seen.update(pull=pull, moved=True)
                return None
            rollup = commit["statusCheckRollup"]
            contexts: dict[str, Any] | None = None if rollup is None else rollup["contexts"]
            return contexts

        def visit(_: dict[str, Any], page: dict[str, Any]) -> None:
            checks.extend(_check(node) for node in page["nodes"])

        self._pages(
            f"checks of {repo}#{pr_number}",
            lambda after: self._graphql(
                PULL_QUERY,
                {
                    "owner": owner,
                    "name": name,
                    "number": pr_number,
                    "first": CONTEXTS_PAGE_SIZE,
                    "after": after,
                },
            ),
            connection,
            visit,
        )
        pull = seen["pull"]
        return {
            "number": pull["number"],
            "head_sha": pull["headRefOid"],
            "commit_oid": "" if seen["moved"] else seen["oid"],
            "mergeable": pull["mergeable"],
            "merge_state_status": pull["mergeStateStatus"],
            "linear_issue": linear_issue_for(
                pull.get("headRefName") or "", pull.get("body") or "", self._linear_team_keys
            ),
            "checks": [] if seen["moved"] else checks,
        }

    def list_merged_prs(self, repo: str, since: str) -> list[dict[str, Any]]:
        _split_repo(repo)
        if not _SINCE.fullmatch(since):
            raise GitHubClientError(f"invalid since {since!r}: expected YYYY-MM-DDTHH:MM:SSZ")
        search = f"repo:{repo} is:pr is:merged merged:>={since}"
        prs: list[dict[str, Any]] = []

        def visit(_: dict[str, Any], page: dict[str, Any]) -> None:
            for node in page["nodes"]:
                # A hit that is not a pull request comes back as an empty object.
                if not node:
                    continue
                prs.append(
                    {
                        "number": node["number"],
                        "head_sha": node["headRefOid"],
                        "merged_at": node["mergedAt"],
                        "linear_issue": linear_issue_for(
                            node.get("headRefName") or "",
                            node.get("body") or "",
                            self._linear_team_keys,
                        ),
                        "reviews": [
                            {
                                "state": review["state"],
                                "submitted_at": review["submittedAt"],
                                # None when GitHub no longer has the reviewed commit.
                                "commit_oid": (review.get("commit") or {}).get("oid"),
                            }
                            for review in node["reviews"]["nodes"]
                        ],
                    }
                )

        self._pages(
            f"merged pull requests of {repo}",
            lambda after: self._graphql(
                MERGED_PULLS_QUERY, {"q": search, "first": MERGED_PULLS_PAGE_SIZE, "after": after}
            ),
            lambda data: data["search"],
            visit,
        )
        return prs

    def rerun_check(self, job_id: int | str, repo: str) -> dict[str, Any]:
        """Rerun one GitHub Actions job. This is the client's only write."""
        owner, name = _split_repo(repo)
        if isinstance(job_id, bool) or not str(job_id).isdecimal():
            raise GitHubClientError(f"invalid job id {job_id!r}: not a GitHub Actions job")
        self._call("--method", "POST", f"repos/{owner}/{name}/actions/jobs/{job_id}/rerun")
        return {"job_id": int(job_id), "requested": True}


def create_github_client(linear_team_keys: Sequence[str] | None = None) -> WatcherGitHubClient:
    """Create the real ``gh``-backed client.

    ``linear_team_keys`` restricts which identifiers in a branch name or PR
    body count as a linked Linear story; see ``linear_issue_for``.
    """
    return _GhApiClient(linear_team_keys)
