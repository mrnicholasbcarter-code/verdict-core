# V1-008 Security, Cleanup, and Launch Review Specification

## Status

Implemented in the `feat/v1-271` worktree for issue #271.

## Problem

The release checklist and security workflow must make launch evidence explicit.
Security and dependency checks must fail closed, and the repository must have a
reviewable record of the checks used to decide whether launch is permitted.

## Requirements

1. The Python security workflow runs `pip-audit` and Bandit as required checks;
   neither may be converted into an advisory step with `|| true` or an
   unbounded vulnerability allowlist.
2. The workflow rejects committed `.env`, private-key, and certificate-shaped
   files.
3. The release checklist records source revision, command/workflow evidence,
   result, limitation, reviewer, and UTC date for each launch gate.
4. The default launch state is `PENDING EVIDENCE`; an empty or advisory result
   cannot be interpreted as approval.
5. Automated tests protect the workflow and checklist contracts.

## Non-goals

This story does not alter runtime routing, credentials, provider configuration,
the live control plane, or release publication. A local clean result is not a
substitute for GitHub-hosted CI, CodeQL/OSV, or a human launch signoff.

## Acceptance criteria

- Security scan, Bandit, dependency review, and launch checklist behavior are
  represented by executable tests and fresh command evidence.
- Repository quality gates pass, or failures are reported with their exact
  cause and limitation.
- The completed branch is reviewed by Sol before commit.
