# Feature Specification: Security Cleanup and Launch Review

**Issue**: #271

## Problem

The release checklist and security workflow must make launch evidence explicit.
Security and dependency checks must fail closed, and the repository must have a
reviewable record of the checks used to decide whether launch is permitted.

## Requirements

1. Bandit, dependency audit, CodeQL, and OSV are explicit non-advisory gates.
2. The Python workflow rejects committed environment, private-key, and
   certificate-shaped files, including `.env.production`, `.crt`, `.p12`, and
   conventional `id_rsa`-style names.
3. The release checklist records source revision, command/workflow evidence,
   result, limitation, reviewer, and UTC date for every launch gate.
4. The default launch state is `PENDING EVIDENCE`; an empty or advisory result
   cannot be interpreted as approval.
5. Tests protect the security workflow and checklist contracts against bypass.

## Non-goals

This story does not alter runtime routing, provider configuration, credentials,
the live control plane, or release publication. A local clean result is not a
substitute for GitHub-hosted CI, CodeQL/OSV, or a human launch signoff.

## Acceptance Criteria

- Security scan, Bandit, dependency review, and launch checklist behavior have
  executable test coverage and fresh command evidence.
- Repository quality gates pass, or their exact failure and limitation is
  reported.
- No credentials are committed or rotated by this feature.
- A Sol review completes before merge.
