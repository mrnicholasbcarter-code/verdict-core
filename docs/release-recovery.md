# Immutable Release Recovery

The synchronized Core release publishes three immutable registry versions and
then creates one GitHub release. No cross-registry transaction exists, so a
runner or registry failure can leave a partial release.

## Fail-safe procedure

1. Stop. Do not blindly rerun the failed workflow, create a replacement tag,
   or publish another package.
2. Preserve the failed Actions run, exact tag commit, downloaded candidate
   artifacts, provenance bundle, and job logs.
3. Query PyPI, npm contracts, npm client, and GitHub Releases independently.
   Record which exact `0.2.0` artifacts exist and compare their digests and
   provenance to the candidate artifacts from the failed run.
4. If no immutable registry write occurred, correct the preflight/account
   configuration and start a newly approved run from the unchanged tag.
5. If any package exists, never overwrite or delete it. An authorized release
   owner must review the evidence and either complete only the missing members
   from the exact same source-bound artifacts or declare the train partial and
   choose a new synchronized version in a separate change.
6. Create the GitHub release only after all three registry versions are
   independently verified. Attach the wheel, sdist, both npm tarballs, digest
   manifest, provenance, and a note describing any recovery.

## Container image

The image is built and smoke-tested before any registry write. It is published
last: the version tag is pushed, then attested, and only then does `:latest`
move. If the run fails after the version tag was pushed:

- **Attestation failed:** the version tag exists without provenance. Do not
  delete or re-push it. Record its digest
  (`docker buildx imagetools inspect ghcr.io/<owner>/verdict-core:<version>`)
  and confirm it equals the digest in this run's `push-image` step log (if it
  does not, stop: see the race note below). Then have the release owner attest
  that exact digest
  (`gh attestation` / `actions/attest-build-provenance` in a manual run bound
  to the same tag commit) before `:latest` is moved by hand.
- **Only the `:latest` move failed:** the version tag is pushed and attested.
  Move `:latest` by hand to that same digest.

`:latest` must never point at an unattested digest.

The "tag is new" check and the push are not atomic. GHCR has no create-only
push, so the check refuses only a tag that is already known to exist. Within
this repository the race is closed by process, not by the registry: release
runs share one concurrency group (`release`, never cancelled midway), and this
workflow is the only publisher of `ghcr.io/<owner>/verdict-core`. If someone
pushed the same version tag by hand during a run, the tag may point at a digest
that this run never built or smoke-tested.

Before any attestation or `:latest` move, compare the tag's current digest
(`docker buildx imagetools inspect ghcr.io/<owner>/verdict-core:<version>`)
with the digest from this run's own push output (the `push-image` step log).
**A mismatch stops recovery.** Do not attest the tag's current digest and do
not move `:latest` until a release owner has independently verified the
intended image's identity and provenance. Treat the version as partial (step 5
of the fail-safe procedure above).

The workflow checks target-version availability and GitHub OIDC prerequisites
before its first publication. Those checks reduce risk but cannot prove the
external npm or PyPI trusted-publisher account linkage. Account configuration
remains a human-gated precondition.

The published packages support Node 18 and newer. Release CI uses Node 22, and
the Vitest 4 development/test toolchain requires Node 20 or newer; Vitest is a
development dependency and is not included in either published npm tarball.
