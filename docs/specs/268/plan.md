# Implementation Plan: Issue #268

1. Record the existing packaging and workflow behavior in the issue research,
   data model, and this plan.
2. Add red tests covering Hatch build commands, attestation permissions/action,
   single tag-triggered Release behavior, immutable release configuration, and
   the existing no-token OIDC invariants.
3. Update both Python release paths to use Hatch, add artifact attestation to
   the single release workflow, and make GitHub Release creation immutable and
   artifact-scoped.
4. Run focused tests, build the wheel and sdist with Hatch, run repository
   quality gates, and inspect the final diff for unrelated changes.
5. Commit with a conventional message and report the commit plus fresh command
   evidence. Sol review is a separate read-only review step before completion.

## Files expected to change

- `.github/workflows/pypi-publish.yml`
- `.github/workflows/release.yml`
- `tests/test_release_workflow.py`
- `docs/specs/268/{spec,research,data-model,plan,tasks}.md`

## Risks and mitigations

- GitHub action input names can drift: tests pin the documented action inputs
  and workflow permissions.
- Existing release workflow publishes several package types: retain npm
  behavior and scope Python changes to the release artifact steps.
- Local environments may lack Hatch: install/use the project build dependency
  only for verification; do not add runtime dependencies.
