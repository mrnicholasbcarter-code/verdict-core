# Versioning Scheme

## Semantic Versioning

We follow [Semantic Versioning 2.0.0](https://semver.org/):

```
MAJOR.MINOR.PATCH
```

### MAJOR (Breaking Changes)
- Incompatible API changes
- Removal of deprecated features
- Major architectural changes
- Database/schema migrations without backward compatibility

### MINOR (New Features)
- New features, backward compatible
- New APIs, endpoints, commands
- New configuration options
- Performance improvements
- New integrations

### PATCH (Bug Fixes)
- Backward compatible bug fixes
- Security patches
- Documentation fixes
- Dependency updates (non-breaking)
- Minor performance improvements

## Pre-Release Versions

### Alpha
- Early development, unstable
- Format: `MAJOR.MINOR.PATCH-alpha.N`
- May have breaking changes between releases

### Beta
- Feature complete, testing
- Format: `MAJOR.MINOR.PATCH-beta.N`
- API stable, may have bugs

### Release Candidate
- Production ready, final testing
- Format: `MAJOR.MINOR.PATCH-rc.N`
- No planned changes unless critical bugs found

## Release Cadence

| Release Type | Frequency | Branch |
|--------------|-----------|--------|
| Patch | As needed | `main` |
| Minor | 6-8 weeks | `main` |
| Major | As needed | `main` |
| Pre-release | As needed | `main` |

## Version Locations

One release version is written in every place below. `scripts/verify_release_versions.py --tag vX.Y.Z`
checks the Python, runtime, contracts and client versions and the client's peer range;
`tests/test_release_workflow.py::test_every_release_version_location_matches_pyproject`
checks all of them, including both lock files.

### Python (verdict-core)
- `pyproject.toml`: `version = "X.Y.Z"`
- `verdict/__init__.py`: `__version__ = "X.Y.Z"`
- `uv.lock`: the `verdict-core` package entry

### TypeScript packages
- `package.json` (workspace root): `"version": "X.Y.Z"`
- `contracts/package.json` (`@bodanglin/verdict-contracts`): `"version": "X.Y.Z"`
- `verdict/client-sdk/package.json` (`@bodanglin/verdict-client`): `"version": "X.Y.Z"`, and
  `"@bodanglin/verdict-contracts": "^X.Y.Z"` in `peerDependencies` and `devDependencies`
- `package-lock.json`: the root, `contracts` and `verdict/client-sdk` entries

### Not bumped with the release
- `install.sh` pins the newest version that is already published on PyPI, with its real
  SHA-256 digests. It is updated only after the release is published.

## Version Bumping

### Automated (Release Workflow)
Triggered by Git tag push:
```bash
git tag vX.Y.Z && git push origin vX.Y.Z
```

### Manual
```bash
# Python
uv version X.Y.Z

# TypeScript (in each package)
npm version patch  # or minor/major
```

## Breaking Change Policy

### When Allowed
- Major version bump required
- Deprecation period of at least 2 minor releases
- Migration guide in CHANGELOG.md
- Deprecation warnings in code (warnings.warn in Python, console.warn in TS)

### Examples of Breaking Changes
- Removing or renaming public APIs
- Changing function signatures
- Removing configuration options
- Changing default behaviors
- Removing deprecated features
- Database/schema changes requiring migration

## Version Constraints

### Python Dependencies
- Use compatible version ranges, for example `>=1.0.0,<2.0.0`
- Exact versions are pinned in `uv.lock`

### TypeScript Dependencies
- Use caret ranges, for example `^1.0.0`
- Peer dependencies for shared types: `peerDependencies`

## Changelog Format

Follow [Keep a Changelog](https://keepachangelog.com/):

```markdown
## [X.Y.Z] - YYYY-MM-DD
### Added
- New feature X
### Fixed
- Bug Y
### Changed
- Modified behavior Z
### Deprecated
- Feature A (will be removed in a later major version)
### Removed
- Old feature B
### Security
- CVE-XXXX-XXXX patched
```

## Tagging Convention

- Release tags: `vMAJOR.MINOR.PATCH` (e.g., `v1.0.0`)
- Pre-release: `vMAJOR.MINOR.PATCH-alpha.1`, `vMAJOR.MINOR.PATCH-beta.1`, `vMAJOR.MINOR.PATCH-rc.1`
- Annotated tags with release notes

## Compatibility Matrix

| Verdict Core | Verdict Contracts | Verdict Client |
|--------------|-------------------|----------------|
| 1.0.x        | 1.0.x             | 1.0.x          |

All packages in a release share the same version.
