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

The workflow checks target-version availability and GitHub OIDC prerequisites
before its first publication. Those checks reduce risk but cannot prove the
external npm or PyPI trusted-publisher account linkage. Account configuration
remains a human-gated precondition.

The published packages support Node 18 and newer. Release CI uses Node 22, and
the Vitest 4 development/test toolchain requires Node 20 or newer; Vitest is a
development dependency and is not included in either published npm tarball.
