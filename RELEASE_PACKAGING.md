# Verdict Core - Release Packaging & Support Documentation

This document describes the release packaging, support posture, and public issue templates for the Verdict project.

## Related Files

```
SUPPORT.md                  # Support policy document (repo root)
RELEASE_CHECKLIST.md        # Release validation checklist (repo root)
RELEASE_PACKAGING.md        # Packaging procedures (this file)
VERSIONING.md               # Versioning scheme (repo root)
.github/ISSUE_TEMPLATE/     # GitHub issue templates
├── bug_report.yml
├── feature_request.yml
├── documentation.yml
├── question.yml
├── reproducibility.yml
└── security_vulnerability.yml
```

## Release Process

### Versioning
We follow [Semantic Versioning](https://semver.org/):
- **MAJOR**: Breaking changes
- **MINOR**: New features, backward compatible
- **PATCH**: Bug fixes, backward compatible

### Release Steps

1. **Prepare Release**
   ```bash
   # Update the version in every location listed in VERSIONING.md
   python scripts/verify_release_versions.py --tag vX.Y.Z
   # Update CHANGELOG.md
   # Run full test suite
   python -m pytest tests/ --ignore=tests/test_vcr_fallback.py -x
   npm test --workspaces
   ```

2. **Build Artifacts**
   ```bash
   # Python
   uv build
   
   # TypeScript
   npm run build --workspaces
   ```

3. **Validate**
   ```bash
   # Python
   uv run twine check dist/*
   
   # TypeScript
   npm pack --dry-run (in each package)
   ```

4. **Publish** (via GitHub Release)
   - Create tag: `git tag vX.Y.Z && git push origin vX.Y.Z`
   - GitHub Actions handles PyPI and npm publishing
   - Artifacts attached to GitHub Release

### Support Policy

See [SUPPORT.md](SUPPORT.md) for:
- Supported versions
- Response times
- Security reporting
- Deprecation policy

## Issue Templates

Located in `.github/ISSUE_TEMPLATE/`:
- **bug_report.yml** - Bug reports
- **feature_request.yml** - Feature requests
- **documentation.yml** - Documentation issues
- **question.yml** - Questions
- **reproducibility.yml** - Reproducibility reports
- **security_vulnerability.yml** - Security vulnerability reports