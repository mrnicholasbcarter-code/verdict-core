# Contributing to Verdict

Thanks for your interest. Here's how to contribute.

## Setup

```bash
git clone https://github.com/mrnicholasbcarter-code/verdict-core.git
cd verdict-core
uv sync --extra dev --extra server --extra dashboard
```

Read [the autonomous-development contract](docs/guides/autonomous-development.md)
for documentation/RAG preflight, Code Review Graph review, ticket ownership,
OmniRoute worker routing, and PR/CI lifecycle requirements.

## Running Tests

```bash
uv run pytest -q
uv run --extra dev --extra dashboard --extra server ruff check .
uv run --extra dev --extra dashboard --extra server ruff format --check .
uv run --extra dev --extra dashboard --extra server mypy verdict --strict
```

The reachability, definition-of-done, proof-path and OpenSpec gates run in
the required `lint` job of `.github/workflows/ci.yml`. That job also applies
`ruff check --fix-only` and `ruff format` to same-repository pull requests,
commits the result and approves the checks GitHub queues for that commit, so a
formatting-only failure needs no extra turn.
`vgate` is operator-owned and separate from these CI gates.
Run the same report locally:

```bash
.venv/bin/python scripts/check_reachability.py
```

It exits 1 on a public symbol or a `verdict/` module that no production code
reaches and that is not in `reachability-baseline.json` /
`reachability-modules-baseline.json`, and on a production reference to a symbol
listed in `reachability-forbidden.json` outside its `allowed_modules`. On pull
requests CI adds `--ratchet-base <base sha>`: both baselines may only shrink, a
fixed entry must leave the baseline, and an `UNTRIAGED` entry in a module the
pull request changes fails until it is wired, deleted or triaged. Integration
trains add `--min-burn N`. Add `--json` for machine-readable output, or
`--skip-vulture` if vulture is not installed locally. See
`docs/quality/REACHABILITY-TRIAGE-2026-10.md` for the triage policy.


## Running the tests from a clean clone

```bash
uv sync --frozen --extra dev --extra server --extra dashboard
.venv/bin/python -m pytest -q
```

Note: Running `pytest` directly from the system will fail collection due to missing `httpx` and other test dependencies. Tests must not depend on PATH binaries like `ruff` or `verdict`—use `.venv/bin/python -m pytest` and `sys.executable` in test commands.

## Pull Requests

1. Fork the repo and create a branch from `main`.
2. Add tests for any new functionality.
3. Ensure the project-environment `pytest`, Ruff, and strict mypy commands all pass.
4. Write a clear PR description explaining what and why.

## Definition of done

The seven-part definition of done is in [AGENTS.md](AGENTS.md#definition-of-done).
`scripts/check_dod.py` enforces it in CI from `docs/proof/dod/<story>.json`.
Merged, green and reviewed are necessary, never sufficient.

Reviewers still ask **"who calls this in production?"** An answer naming only
`tests/` is not a production caller, and `scripts/check_reachability.py` must
not report the new code.

## Design Principles

- **Layered dependencies.** Core routing remains lightweight; the HTTP proxy uses the declared `httpx` dependency and the FastAPI server is installed with the `server` extra.
- **Safety first.** Deterministic policy and capability gates always apply. Managed adaptive
  intelligence is required for production readiness and cannot override hard safety gates.
- **Explicit degradation.** A development-only degraded mode may be used when the managed
  intelligence backend is unavailable. It must be visible in readiness and decision metadata.
- **Decision transparency.** Every routing decision is logged and explainable.

## Code Style

- Ruff for linting and formatting.
- Type hints on all public APIs.
- Docstrings on all public functions and classes.
- Tests live in `tests/` and mirror the `verdict/` structure.

## License

By contributing, you agree that your contributions will be licensed under the MIT License.