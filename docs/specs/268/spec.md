# Issue #268: Python Packaging and Trusted Release Automation

## Status

Approved for implementation in `feat/v1-268`.

## Problem

Verdict has tag-triggered Python publishing, but the release path does not yet
prove that Hatch is the build authority, does not attest the published Python
artifacts, and does not define one auditable tag-to-immutable-GitHub-Release
workflow.

## Requirements

1. A version tag (`v*`) builds exactly one Python wheel and one source
   distribution with Hatch.
2. PyPI publication uses Trusted Publishing OIDC only, with `id-token: write`;
   no long-lived PyPI token is referenced.
3. The built Python distributions receive GitHub artifact attestations.
4. The same tag workflow publishes the attested distributions to PyPI, creates
   one immutable GitHub Release, and attaches the Python distributions and their
   attestations/provenance evidence.
5. Existing npm publication and repository quality gates remain intact.

## Acceptance criteria

- `hatch build` is the Python distribution build command in the release path.
- `.github/workflows/pypi-publish.yml` remains an OIDC-only manual fallback and
  contains no `PYPI_API_TOKEN` or password input.
- A GitHub artifact-attestation action runs after the Python build with the
  required OIDC permissions.
- GitHub Release creation is tag-driven, uses `contents: write`, and checks for
  an existing release before creation so reruns fail instead of mutating it.
- Tests validate the workflow contract and local Hatch artifact contents.
