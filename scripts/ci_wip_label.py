#!/usr/bin/env python3
"""Say whether this CI run is work in progress: its pull request carries the ``wip`` label.

The lint step "Touched OpenSpec changes have no open tasks" lets a pull request with the
``wip`` label merge with open tasks. The run on main after that merge is a ``push`` event.
A push event has no pull request in it, so the label has to be looked up (issue #1412).

* ``pull_request``: the label comes from the event (``--event-wip``, ``true`` or ``false``).
* ``push``: GitHub's "list pull requests associated with a commit" endpoint
  (``GET repos/{owner}/{repo}/commits/{sha}/pulls``) names the pull request that produced
  the pushed commit. The run is wip exactly when that one pull request was merged into the
  pushed branch as that commit and carries the label.
* anything else: not wip.

Only the first page of that answer (100 pull requests) is read; a full page is not trusted
to be complete, so it is "not wip" like any other doubt.

Everything that cannot be established is "not wip", so the strict task check runs: no
pull request for the commit, more than one candidate, a response of an unexpected shape,
a failed or timed-out ``gh`` call. This script never decides that open tasks are fine
by itself; it only reports a label. ``check_openspec_tasks.py --wip`` is unchanged.

Prints ``true`` or ``false`` and exits 0; the reason goes to stderr.

Usage:
    scripts/ci_wip_label.py --event "$GITHUB_EVENT_NAME" --event-wip "$PR_WIP" \\
        --repo "$GITHUB_REPOSITORY" --sha "$GITHUB_SHA" --branch "$GITHUB_REF_NAME"
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable

WIP_LABEL = "wip"
TIMEOUT_SECONDS = 30
# Only the first page is read. A full page may have more behind it, so it is not trusted.
PAGE_SIZE = 100

Fetch = Callable[[str, str], object]


def fetch_pulls(repo: str, sha: str) -> object:
    """The parsed answer of the "pull requests associated with a commit" endpoint.

    Raises ``OSError`` (no ``gh``), ``subprocess.CalledProcessError`` (the API call failed),
    ``subprocess.TimeoutExpired`` or ``ValueError`` (not JSON); the caller treats each as
    "not wip".
    """
    done = subprocess.run(
        [
            "gh",
            "api",
            "-H",
            "X-GitHub-Api-Version: 2022-11-28",
            f"repos/{repo}/commits/{sha}/pulls?per_page={PAGE_SIZE}",
        ],
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        check=True,
    )
    return json.loads(done.stdout)


def _produced(pull: object, *, sha: str, branch: str) -> dict[str, object] | None:
    """``pull`` when it is a pull request merged into ``branch`` as the commit ``sha``."""
    if not isinstance(pull, dict) or not pull.get("merged_at"):
        return None
    base = pull.get("base")
    if pull.get("merge_commit_sha") != sha or not isinstance(base, dict):
        return None
    return pull if base.get("ref") == branch else None


def _has_wip_label(pull: dict[str, object]) -> bool:
    labels = pull.get("labels")
    if not isinstance(labels, list):
        return False
    names = (label.get("name") for label in labels if isinstance(label, dict))
    # GitHub's `contains()` in the pull request run compares without regard to case.
    return any(isinstance(name, str) and name.casefold() == WIP_LABEL for name in names)


def _decide(
    event: str, event_wip: str, repo: str, sha: str, branch: str, fetch: Fetch
) -> tuple[bool, str]:
    if event == "pull_request":
        wip = event_wip == "true"
        return wip, f"pull_request event: the pull request {'has' if wip else 'lacks'} the label"
    if event != "push":
        return False, f"{event or 'unknown'} event: not wip"
    if not repo or not sha:
        return False, "push event without a repository or commit: not wip"
    try:
        pulls = fetch(repo, sha)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return (
            False,
            f"could not list the pull requests of {sha[:8]} ({type(exc).__name__}): not wip",
        )
    if not isinstance(pulls, list):
        return False, f"unexpected answer for the pull requests of {sha[:8]}: not wip"
    if len(pulls) >= PAGE_SIZE:
        return (
            False,
            f"{len(pulls)} pull requests for {sha[:8]} fill the first page; may be more: not wip",
        )
    produced = [p for p in (_produced(pull, sha=sha, branch=branch) for pull in pulls) if p]
    if len(produced) != 1:
        return (
            False,
            f"{len(produced)} merged pull request(s) produced {sha[:8]} on {branch}: not wip",
        )
    number = produced[0].get("number")
    if _has_wip_label(produced[0]):
        return True, f"pull request #{number} produced {sha[:8]} and carries the {WIP_LABEL} label"
    return False, f"pull request #{number} produced {sha[:8]} and has no {WIP_LABEL} label: not wip"


def is_wip(
    event: str, *, event_wip: str, repo: str, sha: str, branch: str, fetch: Fetch = fetch_pulls
) -> bool:
    """True when the run belongs to a pull request that carries the ``wip`` label."""
    return _decide(event, event_wip, repo, sha, branch, fetch)[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--event", required=True, help="the workflow event name")
    parser.add_argument("--event-wip", default="false", help="the event's own label: true or false")
    parser.add_argument("--repo", default="", help="owner/name")
    parser.add_argument("--sha", default="", help="the pushed commit")
    parser.add_argument("--branch", default="", help="the pushed branch")
    args = parser.parse_args(argv)
    wip, reason = _decide(args.event, args.event_wip, args.repo, args.sha, args.branch, fetch_pulls)
    print(reason, file=sys.stderr)
    print("true" if wip else "false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
