# Release Checklist

## Pre-Release Validation

## Evidence-bound launch signoff

Record the exact source revision, command or workflow URL, result, limitation,
reviewer, and UTC date for every checked gate. An unchecked or advisory result is
not a launch approval.

| Gate | Source revision | Evidence URL/command (hosted URL required for CodeQL/OSV) | Result | Limitation | Reviewer/date |
|---|---|---|---|---|---|
| CI |  |  |  |  |  |
| Bandit |  |  |  |  |  |
| Dependency audit |  |  |  |  |  |
| Committed credential-file scan |  |  |  |  |  |
| npm production dependency audit |  |  |  |  |  |
| CodeQL |  |  |  |  |  |
| OSV |  |  |  |  |  |
| Wheel/sdist smoke |  |  |  |  |  |
| Documentation smoke |  |  |  |  |  |

Launch decision: **PENDING EVIDENCE**

### Python Package (verdict-core)
- [ ] All tests pass: `python -m pytest tests/ --ignore=tests/test_vcr_fallback.py -x`
- [ ] Code quality: `ruff check . && mypy --strict verdict/`
- [ ] Security scan: `bandit -r verdict/`
- [ ] Package builds: `uv build` produces wheel and sdist
- [ ] Package metadata: `python -m twine check dist/*` (as in the `ci.yml` build job)
- [ ] Clean install: `pip install dist/*.whl` in fresh venv works
- [ ] CLI works: `verdict --help` after install
- [ ] API server: `verdict serve` starts without errors
- [ ] Quickstart passes: `python scripts/quickstart.py`

### TypeScript Packages
- [ ] verdict-contracts: `npm run build && npm test`
- [ ] verdict-client: `npm run build && npm test`
- [ ] Clean install: `npm ci` succeeds
- [ ] npm pack dry-run: `npm pack --dry-run` produces expected files

### Cross-Language Parity
- [ ] Contract tests pass in both Python and TypeScript
- [ ] Contract fixtures validate identically in both languages
- [ ] Parity report reviewed: [`CONTRACT_PARITY.md`](CONTRACT_PARITY.md) matches `contracts/src/index.ts`

### Benchmarks
- [ ] Reproducible benchmarks pass: `uv run verdict benchmark`
- [ ] Benchmark results indexed in `docs/benchmarks/README.md`

### Evidence & Documentation
- [ ] Evidence bundle generated: `python scripts/evidence_bundle.py`
- [ ] Security evidence: threat model, privacy policy, supply chain
- [ ] Benchmark results: quality/cost/latency/availability
- [ ] Quickstart verified: `python scripts/quickstart.py`
- [ ] README claims match verified behavior

### Release Artifacts
- [ ] Version bumped in every location in [VERSIONING.md](VERSIONING.md#version-locations); `python scripts/verify_release_versions.py --tag vX.Y.Z` passes
- [ ] `install.sh` pin updated with real PyPI digests after publication (not before)
- [ ] CHANGELOG.md updated with Keep a Changelog format
- [ ] Git tag created: `vX.Y.Z`
- [ ] GitHub Release created with artifacts
- [ ] PyPI publish: `uv publish` or trusted publishing
- [ ] npm publish: `npm publish --access public --provenance`
- [ ] Docker image built and pushed (if applicable)

## Issue templates

Public issue forms live in `.github/ISSUE_TEMPLATE/`: `bug_report.yml`,
`feature_request.yml`, `documentation.yml`, `question.yml`, `reproducibility.yml`
and `security_vulnerability.yml`. Support policy: [SUPPORT.md](SUPPORT.md).

## Post-Release
- [ ] Verify installations work from PyPI/npm
- [ ] Update docs site if needed
- [ ] Announce in release notes
- [ ] Update support matrix if version drops support
