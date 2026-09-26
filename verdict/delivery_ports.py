"""Concrete GitHub/Linear ports for autonomous delivery (live ``gh`` / local evidence).

These adapters are transport only. Policy stays in ``verdict.delivery``.
Secrets are never logged; subprocess output is bounded.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from verdict.delivery import DeliveryError

__all__ = ["GhCliPort", "LinearEvidencePort", "run_gh_json"]

_DEFAULT_TIMEOUT_S = 60
_MAX_OUTPUT_BYTES = 1_000_000


def run_gh_json(args: Sequence[str], *, timeout_s: float = _DEFAULT_TIMEOUT_S) -> Any:
    """Run ``gh`` and parse JSON stdout. Raises DeliveryError on failure."""
    cmd = ["gh", *list(args)]
    try:
        completed = subprocess.run(cmd, check=False, capture_output=True, timeout=timeout_s)
    except FileNotFoundError as exc:
        raise DeliveryError("gh CLI not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise DeliveryError(f"gh timed out after {timeout_s}s: {' '.join(cmd)}") from exc

    if completed.returncode != 0:
        stderr = (completed.stderr or b"")[:2000].decode("utf-8", errors="replace")
        raise DeliveryError(f"gh failed ({completed.returncode}): {stderr.strip()}")

    raw = completed.stdout or b""
    if len(raw) > _MAX_OUTPUT_BYTES:
        raise DeliveryError("gh output exceeded bound")
    text = raw.decode("utf-8")
    if not text.strip():
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise DeliveryError("gh returned non-JSON output") from exc


def _normalize_check_item(item: Mapping[str, Any]) -> dict[str, Any]:
    """Map ``gh pr checks --json`` rows into prime_state classify_ci shape."""
    bucket = str(item.get("bucket") or "")
    state = str(item.get("state") or "")
    if bucket == "pass" or state.upper() in {"SUCCESS", "SKIPPED"}:
        status, conclusion = "COMPLETED", "SUCCESS" if bucket != "skipping" else "SKIPPED"
    elif bucket == "fail" or state.upper() in {"FAILURE", "ERROR", "TIMED_OUT"}:
        status, conclusion = "COMPLETED", "FAILURE"
    elif bucket == "pending" or state.upper() in {"PENDING", "QUEUED", "IN_PROGRESS"}:
        status, conclusion = "IN_PROGRESS", None
    else:
        status, conclusion = "COMPLETED", state.upper() or None
    return {
        "name": item.get("name") or item.get("workflow") or "unknown",
        "status": status,
        "conclusion": conclusion,
        "required": True,
    }


class GhCliPort:
    """Live GitHubPort backed by the official ``gh`` CLI."""

    def __init__(self, *, repo: str | None = None, timeout_s: float = _DEFAULT_TIMEOUT_S) -> None:
        self.repo = repo
        self.timeout_s = timeout_s

    def _repo_args(self) -> list[str]:
        return ["--repo", self.repo] if self.repo else []

    def create_pull_request(self, *, title: str, body: str, head: str, base: str) -> str:
        payload = run_gh_json(
            [
                "pr",
                "create",
                *self._repo_args(),
                "--title",
                title,
                "--body",
                body,
                "--head",
                head,
                "--base",
                base,
                "--json",
                "url",
            ],
            timeout_s=self.timeout_s,
        )
        if not isinstance(payload, dict) or not payload.get("url"):
            raise DeliveryError("gh pr create did not return a url")
        return str(payload["url"])

    def list_required_checks(
        self, *, head_sha: str, pr_url: str | None = None
    ) -> list[dict[str, Any]]:
        """List checks for a PR URL (preferred) or commit SHA via Checks API."""
        if pr_url:
            payload = run_gh_json(
                [
                    "pr",
                    "checks",
                    pr_url,
                    *self._repo_args(),
                    "--json",
                    "name,state,bucket,workflow",
                ],
                timeout_s=self.timeout_s,
            )
            if isinstance(payload, list):
                return [_normalize_check_item(item) for item in payload if isinstance(item, dict)]
            return []

        if not self.repo:
            raise DeliveryError("list_required_checks without pr_url requires repo=")
        owner, _, name = self.repo.partition("/")
        payload = run_gh_json(
            ["api", f"repos/{owner}/{name}/commits/{head_sha}/check-runs", "--paginate"],
            timeout_s=self.timeout_s,
        )
        runs: list[Any] = []
        if isinstance(payload, dict):
            runs = list(payload.get("check_runs") or [])
        elif isinstance(payload, list):
            for page in payload:
                if isinstance(page, dict):
                    runs.extend(page.get("check_runs") or [])
        return [
            {
                "name": run.get("name"),
                "status": run.get("status") or "COMPLETED",
                "conclusion": run.get("conclusion"),
                "required": True,
            }
            for run in runs
            if isinstance(run, dict)
        ]

    def get_mergeability(self, *, pr_url: str) -> tuple[str, str]:
        payload = run_gh_json(
            ["pr", "view", pr_url, *self._repo_args(), "--json", "mergeable,mergeStateStatus"],
            timeout_s=self.timeout_s,
        )
        if not isinstance(payload, dict):
            raise DeliveryError("gh pr view mergeability failed")
        return str(payload.get("mergeable") or "UNKNOWN"), str(
            payload.get("mergeStateStatus") or "UNKNOWN"
        )

    def merge_pull_request(self, *, pr_url: str, method: str = "squash") -> str:
        if method != "squash":
            raise DeliveryError("BOD-68 only permits squash merges")
        # merge may return empty stdout; then re-query mergeCommit
        try:
            run_gh_json(
                ["pr", "merge", pr_url, *self._repo_args(), "--squash", "--delete-branch"],
                timeout_s=self.timeout_s,
            )
        except DeliveryError as exc:
            # gh pr merge often returns non-JSON; tolerate and re-query
            if "non-JSON" not in str(exc) and "gh failed" not in str(exc):
                raise
            subprocess.run(
                ["gh", "pr", "merge", pr_url, *self._repo_args(), "--squash", "--delete-branch"],
                check=True,
                capture_output=True,
                timeout=self.timeout_s,
            )
        payload = run_gh_json(
            ["pr", "view", pr_url, *self._repo_args(), "--json", "mergeCommit,state"],
            timeout_s=self.timeout_s,
        )
        if isinstance(payload, dict):
            commit = payload.get("mergeCommit") or {}
            if isinstance(commit, dict) and commit.get("oid"):
                return str(commit["oid"])
        raise DeliveryError("merge completed but merge SHA missing")

    def list_main_checks(self, *, main_sha: str) -> list[dict[str, Any]]:
        return self.list_required_checks(head_sha=main_sha)

    def list_unresolved_review_threads(self, *, pr_url: str) -> list[str]:
        query = (
            "query($url: URI!) {"
            "  resource(url: $url) {"
            "    ... on PullRequest {"
            "      reviewThreads(first: 50) { nodes { isResolved id } }"
            "    }"
            "  }"
            "}"
        )
        payload = run_gh_json(
            ["api", "graphql", "-f", f"query={query}", "-F", f"url={pr_url}"],
            timeout_s=self.timeout_s,
        )
        try:
            nodes = payload["data"]["resource"]["reviewThreads"]["nodes"]
        except (TypeError, KeyError, AttributeError):
            return []
        return [
            str(node.get("id") or "thread")
            for node in (nodes if isinstance(nodes, list) else [])
            if isinstance(node, dict) and node.get("isResolved") is False
        ]


class LinearEvidencePort:
    """Durable local evidence port when live Linear API is not injected.

    Writes JSON receipts under ``evidence_dir`` so Done closeout remains
    auditable. Production should inject a real Linear client that marks Done.
    """

    def __init__(self, *, evidence_dir: str | Path) -> None:
        self.evidence_dir = Path(evidence_dir)

    def record_delivery_evidence(
        self, *, issue: str, pr_url: str, merge_sha: str, evidence: Mapping[str, Any]
    ) -> None:
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        receipt = {
            "issue": issue,
            "pr_url": pr_url,
            "merge_sha": merge_sha,
            "evidence": dict(evidence),
        }
        target = self.evidence_dir / f"{issue.replace('/', '_')}-delivery.json"
        target.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def mark_done(self, *, issue: str) -> None:
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        marker = self.evidence_dir / f"{issue.replace('/', '_')}-done.marker"
        marker.write_text(issue + "\n", encoding="utf-8")
