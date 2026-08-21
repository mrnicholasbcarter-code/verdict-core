# Release Checklist

## Pre-Release Validation

## Evidence-bound launch signoff

Record the exact source revision, command or workflow URL, result, limitation,
reviewer, and UTC date for every checked gate. An unchecked or advisory result is
not a launch approval.

| Gate | Source revision | Evidence URL/command | Result | Limitation | Reviewer/date |
|---|---|---|---|---|---|
| CI |  |  |  |  |  |
| Bandit |  |  |  |  |  |
| Dependency audit |  |  |  |  |  |
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
- [ ] Clean install: `pip install dist/*.whl` in fresh venv works
- [ ] CLI works: `verdict --help` after install
- [ ] API server: `verdict serve` starts without errors
- [ ] Quickstart passes: `python scripts/quickstart.py`

### TypeScript Packages
- [ ] verdict-contracts: `npm run build && npm test`
- [ ] verdict-client: `npm run build && npm test`
- [ ] verdict-node: `npm run build && npm test && npm run format:check`
- [ ] Clean install: `npm ci` succeeds
- [ ] npm pack dry-run: `npm pack --dry-run` produces expected files

### Cross-Language Parity
- [ ] Contract tests pass in both Python and TypeScript
- [ ] Contract fixtures validate identically in both languages
- [ ] Parity report generated: `npx tsx scripts/parity.ts` passes

### Benchmarks
- [ ] Reproducible benchmarks pass: `python -m verdict.benchmarking`
- [ ] Benchmark results committed to evidence/

### Evidence & Documentation
- [ ] Evidence bundle generated: `python scripts/evidence_bundle.py`
- [ ] Security evidence: threat model, privacy policy, supply chain
- [ ] Benchmark results: quality/cost/latency/availability
- [ ] Quickstart verified: `python scripts/quickstart.py`
- [ ] README claims match verified behavior

### Release Artifacts
- [ ] Version bumped in pyproject.toml, package.json files
- [ ] CHANGELOG.md updated with Keep a Changelog format
- [ ] Git tag created: `vX.Y.Z`
- [ ] GitHub Release created with artifacts
- [ ] PyPI publish: `uv publish` or trusted publishing
- [ ] npm publish: `npm publish --access public --provenance`
- [ ] Docker image built and pushed (if applicable)

## Post-Release
- [ ] Verify installations work from PyPI/npm
- [ ] Update docs site if needed
- [ ] Announce in release notes
- [ ] Update support matrix if version drops support
