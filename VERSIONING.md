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

### Python (verdict-core)
- `pyproject.toml`: `version = "0.1.0"`
- `__init__.py`: `__version__ = "0.1.0"`

### TypeScript Packages
- `contracts/package.json`: `"version": "0.1.0"`
- `verdict/client-sdk/package.json`: `"version": "0.1.0"`
- `verdict-node/package.json`: `"version": "0.1.0"`

## Version Bumping

### Automated (Release Workflow)
Triggered by Git tag push:
```bash
git tag v0.1.0 && git push origin v0.1.0
```

### Manual
```bash
# Python
uv version 0.1.1

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
- Use compatible version ranges: `>=0.1.0,<0.2.0`
- Pin exact versions in requirements-lock.txt

### TypeScript Dependencies
- Use caret ranges: `^0.1.0`
- Peer dependencies for shared types: `peerDependencies`

## Changelog Format

Follow [Keep a Changelog](https://keepachangelog.com/):

```markdown
## [0.1.1] - 2026-07-26
### Added
- New feature X
### Fixed
- Bug Y
### Changed
- Modified behavior Z
### Deprecated
- Feature A (will be removed in 0.2.0)
### Removed
- Old feature B
### Security
- CVE-XXXX-XXXX patched
```

## Tagging Convention

- Release tags: `vMAJOR.MINOR.PATCH` (e.g., `v0.1.0`)
- Pre-release: `vMAJOR.MINOR.PATCH-alpha.1`, `vMAJOR.MINOR.PATCH-beta.1`, `vMAJOR.MINOR.PATCH-rc.1`
- Annotated tags with release notes

## Compatibility Matrix

| Verdict Core | Verdict Contracts | Verdict Client | Verdict Node |
|--------------|-------------------|----------------|--------------|
| 0.1.x        | 0.1.x             | 0.1.x          | 0.1.x        |

All packages in a release share the same version prefix.