"""Independent OpenCodeReview: independent, fail-closed semantic review via OpenCodeReview (``ocr``).

``OpenCodeReviewer`` runs Alibaba's ``open-code-review`` CLI (v1.12.x) as an
independent semantic review gate that implements the :class:`Reviewer` protocol
from :mod:`verdict.orchestration.contracts`.

Independence and fail-closed behaviour are the whole point:

* The reviewer model is selected fresh, with the implementers' routes/families
  excluded, so the review is never done by a model that wrote the code.
* The gate never returns ``PASS`` unless the review really ran and is clean. A
  non-zero exit, timeout, missing/unparseable output, or an "every item failed"
  run all become ``ERROR`` (never ``PASS``).

The user's global ``ocr`` config is never mutated: the CLI stores config under
``$HOME/.opencodereview/config.json``, so each run gets an isolated ``HOME``
inside ``out_dir`` that is populated with a temporary provider config (including
the API key) and deleted afterwards. The API key is read from an environment
variable at runtime and never written into the repo, argv, or logs.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from verdict.orchestration.contracts import (
    FailureClassification,
    ModelSelector,
    ReviewFinding,
    ReviewResult,
    TaskRequirements,
)

__all__ = ["OcrRun", "OcrRunner", "OpenCodeReviewer", "StaticDiffGate"]

# OCR severity vocabulary -> Verdict's normalized ladder.
_SEVERITY_MAP: Mapping[str, str] = {
    "critical": "critical",
    "blocker": "critical",
    "fatal": "critical",
    "high": "high",
    "error": "high",
    "major": "high",
    "medium": "medium",
    "moderate": "medium",
    "warning": "medium",
    "warn": "medium",
    "low": "low",
    "minor": "low",
    "info": "info",
    "informational": "info",
    "note": "info",
    "notice": "info",
    "suggestion": "info",
}
_VALID_SEVERITY = frozenset({"critical", "high", "medium", "low", "info"})


def _normalize_severity(raw: object) -> str:
    text = str(raw or "").strip().lower()
    return _SEVERITY_MAP.get(text, "info")


@dataclass(frozen=True)
class OcrRun:
    """Terminal result of one ``ocr`` subprocess invocation."""

    exit_code: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False


class OcrRunner(Protocol):
    """Runs one ``ocr`` argv to a terminal :class:`OcrRun`.

    Implementations must never raise for process/transport failures and must
    enforce ``timeout`` themselves (returning ``timed_out=True`` on expiry).
    ``env`` carries the isolated ``HOME`` and must be applied to the child.
    """

    def __call__(
        self, argv: Sequence[str], *, env: Mapping[str, str], timeout: float
    ) -> OcrRun: ...


def _subprocess_runner(argv: Sequence[str], *, env: Mapping[str, str], timeout: float) -> OcrRun:
    """Default runner: run ``ocr`` as a child process with a hard timeout."""
    import os

    child_env = {**os.environ, **env}
    try:
        completed = subprocess.run(
            list(argv), env=child_env, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as exc:
        return OcrRun(
            exit_code=124,
            stdout=exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or ""),
            stderr=exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or ""),
            timed_out=True,
        )
    except (OSError, ValueError) as exc:
        return OcrRun(exit_code=127, stderr=str(exc))
    return OcrRun(
        exit_code=completed.returncode, stdout=completed.stdout or "", stderr=completed.stderr or ""
    )


class OpenCodeReviewer:
    """Independent, fail-closed semantic reviewer backed by the ``ocr`` CLI."""

    _REVIEWER_PREFIX = "open-code-review"
    _PROVIDER = "verdict-omniroute"

    def __init__(
        self,
        selector: ModelSelector,
        *,
        ocr_bin: str = "ocr",
        gateway_url: str = "http://127.0.0.1:20128/v1",
        api_key_env: str = "VERDICT_OMNIROUTE_API_KEY",
        effort: str = "low",
        timeout_seconds: float = 900,
        token_budget: int = 400_000,
        max_reviewer_attempts: int = 3,
        out_dir: Path,
        runner: OcrRunner | None = None,
    ) -> None:
        self._selector = selector
        self._ocr_bin = ocr_bin
        self._gateway_url = gateway_url
        self._api_key_env = api_key_env
        self._effort = effort
        self._timeout_seconds = float(timeout_seconds)
        self._token_budget = int(token_budget)
        self._max_reviewer_attempts = max(1, int(max_reviewer_attempts))
        self._out_dir = Path(out_dir)
        self._runner: OcrRunner = runner or _subprocess_runner

    async def review(
        self,
        *,
        repo: Path,
        base_ref: str,
        head_ref: str,
        background: str,
        exclude_routes: frozenset[str],
        exclude_families: frozenset[str],
    ) -> ReviewResult:
        now = datetime.now(timezone.utc)
        requirements = TaskRequirements(
            required_capabilities=frozenset({"tools"}),
            coding=True,
            reasoning=True,
            frontier_worthy=True,
            exclude_routes=exclude_routes,
            exclude_families=exclude_families,
        )
        tried: set[str] = set()
        last: ReviewResult | None = None
        for _attempt in range(self._max_reviewer_attempts):
            chosen, _verdicts = self._selector.select(
                replace(requirements, exclude_routes=frozenset(exclude_routes | tried)), now=now
            )
            if chosen is None:
                break
            result = await self._review_once(
                chosen.route_id,
                repo=repo,
                base_ref=base_ref,
                head_ref=head_ref,
                background=background,
            )
            if result.status != "ERROR" or not self._provider_failure(result):
                return result
            # The REVIEWER's provider failed (504/timeout/quota), not the review:
            # cool the route down and reselect another independent reviewer.
            last = result
            tried.add(chosen.route_id)
            self._selector.record_failure(
                chosen.route_id,
                FailureClassification("upstream_temporary", "REROUTE", 300, "route", result.detail),
                now=now,
            )
        if last is not None:
            return replace(
                last, detail=f"{last.detail}; reviewer pool exhausted after {len(tried)}"
            )
        return ReviewResult(
            status="ERROR",
            reviewer=self._REVIEWER_PREFIX,
            route_id="",
            detail="no independent reviewer eligible",
        )

    async def _review_once(
        self, route_id: str, *, repo: Path, base_ref: str, head_ref: str, background: str
    ) -> ReviewResult:

        self._out_dir.mkdir(parents=True, exist_ok=True)
        raw_path = self._out_dir / "ocr-raw.json"
        home_dir = self._out_dir / f".ocr-home-{uuid.uuid4().hex}"
        try:
            api_key = self._read_api_key()
        except KeyError as exc:
            return ReviewResult(
                status="ERROR", reviewer=self._REVIEWER_PREFIX, route_id=route_id, detail=str(exc)
            )

        try:
            self._write_isolated_config(home_dir, api_key)
            argv = [
                self._ocr_bin,
                "review",
                "--repo",
                str(repo),
                "--from",
                base_ref,
                "--to",
                head_ref,
                "--format",
                "json",
                "--audience",
                "agent",
                "--effort",
                self._effort,
                "--max-tokens-budget",
                str(self._token_budget),
                "--provider",
                self._PROVIDER,
                "--model",
                route_id,
                "--background",
                background,
                "--output",
                str(raw_path),
            ]
            run = self._runner(argv, env={"HOME": str(home_dir)}, timeout=self._timeout_seconds)
        finally:
            shutil.rmtree(home_dir, ignore_errors=True)

        return self._interpret(run, raw_path, route_id)

    # -- internals ---------------------------------------------------------

    def _read_api_key(self) -> str:
        import os

        value = os.environ.get(self._api_key_env)
        if not value:
            raise KeyError(f"missing API key env {self._api_key_env}")
        return value

    def _write_isolated_config(self, home_dir: Path, api_key: str) -> None:
        cfg_dir = home_dir / ".opencodereview"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        config = {
            "custom_providers": {
                self._PROVIDER: {"url": self._gateway_url, "protocol": "openai", "api_key": api_key}
            },
            "llm": {},
        }
        (cfg_dir / "config.json").write_text(json.dumps(config, indent=2))

    def _interpret(self, run: OcrRun, raw_path: Path, route_id: str) -> ReviewResult:
        raw_ref = str(raw_path)
        if run.timed_out:
            return self._error(route_id, raw_ref, "ocr review timed out")
        if run.exit_code != 0:
            return self._error(route_id, raw_ref, f"ocr review exited {run.exit_code}")
        if not raw_path.exists():
            return self._error(route_id, raw_ref, "ocr output file missing")
        try:
            payload = json.loads(raw_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            return self._error(route_id, raw_ref, f"unparseable ocr output: {exc}")
        if not isinstance(payload, dict):
            return self._error(route_id, raw_ref, "ocr output was not an object")

        if self._every_item_failed(payload):
            return self._error(route_id, raw_ref, "every reviewed item failed")

        findings = self._parse_findings(payload)
        reviewer = self._reviewer_label(payload)
        status = "FAIL" if any(f.blocking() for f in findings) else "PASS"
        return ReviewResult(
            status=status,
            reviewer=reviewer,
            route_id=route_id,
            findings=tuple(findings),
            raw_ref=raw_ref,
        )

    @staticmethod
    def _provider_failure(result: ReviewResult) -> bool:
        detail = result.detail.lower()
        return any(
            token in detail
            for token in (
                "every reviewed item failed",
                "exited",
                "timed out",
                "provider",
                "504",
                "429",
                "503",
            )
        )

    def _error(self, route_id: str, raw_ref: str, detail: str) -> ReviewResult:
        return ReviewResult(
            status="ERROR",
            reviewer=self._REVIEWER_PREFIX,
            route_id=route_id,
            raw_ref=raw_ref,
            detail=detail,
        )

    @staticmethod
    def _every_item_failed(payload: Mapping[str, object]) -> bool:
        manifest = payload.get("manifest")
        if not isinstance(manifest, Mapping):
            return False
        coverage = manifest.get("coverage")
        if not isinstance(coverage, Mapping):
            return False
        selected = coverage.get("selected")
        failed = coverage.get("failed")
        if not isinstance(selected, list) or not selected:
            return False
        if not isinstance(failed, list):
            return False
        return len(failed) >= len(selected)

    @staticmethod
    def _parse_findings(payload: Mapping[str, object]) -> list[ReviewFinding]:
        comments = payload.get("comments")
        findings: list[ReviewFinding] = []
        if not isinstance(comments, list):
            return findings
        for item in comments:
            if not isinstance(item, Mapping):
                continue
            line_raw = item.get("start_line", item.get("line"))
            line: int | None
            try:
                line = int(line_raw) if line_raw is not None else None
            except (TypeError, ValueError):
                line = None
            findings.append(
                ReviewFinding(
                    severity=_normalize_severity(item.get("severity")),
                    category=str(item.get("category", "") or ""),
                    file=str(item.get("path", item.get("file", "")) or ""),
                    line=line,
                    message=str(item.get("content", item.get("message", "")) or ""),
                )
            )
        return findings

    def _reviewer_label(self, payload: Mapping[str, object]) -> str:
        version = self._resolve_version(payload)
        return f"{self._REVIEWER_PREFIX} {version}".strip()

    def _resolve_version(self, payload: Mapping[str, object]) -> str:
        manifest = payload.get("manifest")
        if isinstance(manifest, Mapping):
            execution = manifest.get("execution")
            if isinstance(execution, Mapping):
                version = execution.get("ocr_version")
                if version:
                    return str(version)
        return self._probe_version()

    def _probe_version(self) -> str:
        run = self._runner([self._ocr_bin, "--version"], env={}, timeout=30)
        first = (run.stdout or run.stderr).strip().splitlines()
        if not first:
            return "unknown"
        # e.g. "open-code-review v1.12.9 (bccbc15) linux/amd64"
        parts = first[0].split()
        for token in parts:
            if token.startswith("v") and token[1:2].isdigit():
                return token
        return parts[-1] if parts else "unknown"


def _norm_owned(path: str) -> str:
    return str(path).strip().replace("\\", "/").lstrip("./")


class StaticDiffGate:
    """Cheap deterministic ownership barrier over a git diff.

    FAILs when any file changed between ``base_ref`` and ``head_ref`` falls
    outside the allowed-owned set. This is a fast pre-review guard, not a
    semantic review, so it never calls an LLM.
    """

    _REVIEWER = "static-diff-gate"

    def __init__(self, allowed: Iterable[str], *, differ: DiffLister | None = None) -> None:
        self._allowed = tuple(_norm_owned(p) for p in allowed)
        self._differ: DiffLister = differ or _git_diff_names

    def review(self, *, repo: Path, base_ref: str, head_ref: str) -> ReviewResult:
        try:
            changed = self._differ(repo=repo, base_ref=base_ref, head_ref=head_ref)
        except Exception as exc:
            return ReviewResult(
                status="ERROR",
                reviewer=self._REVIEWER,
                route_id="",
                detail=f"could not compute diff: {exc}",
            )
        violations = [f for f in changed if not self._is_allowed(f)]
        if violations:
            findings = tuple(
                ReviewFinding(
                    severity="high",
                    category="ownership",
                    file=path,
                    line=None,
                    message=f"{path} is outside the allowed-owned set",
                )
                for path in sorted(violations)
            )
            return ReviewResult(
                status="FAIL",
                reviewer=self._REVIEWER,
                route_id="",
                findings=findings,
                detail=f"{len(violations)} file(s) outside ownership barrier",
            )
        return ReviewResult(status="PASS", reviewer=self._REVIEWER, route_id="")

    def _is_allowed(self, path: str) -> bool:
        candidate = _norm_owned(path)
        for owned in self._allowed:
            if candidate == owned or candidate.startswith(owned.rstrip("/") + "/"):
                return True
        return False


class DiffLister(Protocol):
    def __call__(self, *, repo: Path, base_ref: str, head_ref: str) -> Sequence[str]: ...


def _git_diff_names(*, repo: Path, base_ref: str, head_ref: str) -> Sequence[str]:
    completed = subprocess.run(
        ["git", "-C", str(repo), "diff", "--name-only", f"{base_ref}..{head_ref}"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in completed.stdout.splitlines() if line.strip()]
