# Research: Issue #268

## Existing capability inventory

- `pyproject.toml` already uses Hatchling as its PEP 517 backend and declares
  the `verdict` package plus versioned JSON schemas as wheel content.
- `.github/workflows/pypi-publish.yml` already provides tag-triggered OIDC
  publication, but builds with `python -m build` and does not attest artifacts.
- `.github/workflows/release.yml` already creates a GitHub Release and publishes
  npm packages, but independently builds Python artifacts with `uv build` and
  does not attest them.
- `scripts/verify_release_artifacts.py` provides an existing isolated install
  smoke test for exactly one wheel and one sdist.
- Existing `tests/test_release_workflow.py` verifies the OIDC/no-token subset.

## Alternatives considered

- Reuse `python -m build`: rejected because the issue explicitly requires
  Hatch as the build interface.
- Keep separate Python and release tag workflows: rejected because separate
  builders can produce divergent artifacts and weaken the one-tag evidence
  chain.
- Use a PyPI API token: rejected; Trusted Publishing OIDC is an explicit
  acceptance criterion.
- Add a custom signing implementation: rejected; GitHub's official artifact
  attestation action is the existing platform capability.

## Decision

EXTEND the existing tag release workflow and OIDC publisher. The release
workflow becomes the single Python build/attestation/Release evidence producer;
the PyPI workflow remains the narrowly scoped OIDC consumer of the same tag but
must build with Hatch as well so its uploaded distributions are independently
reproducible.

## Constraints

- No credentials are added.
- No Ruflo, OmniRoute routing, autopilot, or other worktree is used.
- No application runtime code changes are needed.
