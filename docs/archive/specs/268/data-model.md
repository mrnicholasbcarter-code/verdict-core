# Data Model: Release Artifacts and Evidence

```text
ReleaseTag
  name: v<version>
  event: push.tags

PythonDistributionSet
  wheel: dist/*.whl (exactly one)
  sdist: dist/*.tar.gz (exactly one)
  builder: hatch

ArtifactAttestation
  subject_path: dist/
  predicate: GitHub artifact attestation
  issuer: GitHub Actions OIDC

ImmutableGitHubRelease
  tag: ReleaseTag.name
  contents_permission: write
  fail_on_unmatched_files: true
  files: PythonDistributionSet + attestation evidence
```

The two distribution files are the normative release artifacts. Attestation
metadata is provenance evidence and must be generated from those exact files;
the release action must not overwrite an existing release for the same tag.
