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

Run the reachability report locally (BOD-316) the same way CI does:

```bash
.venv/bin/python scripts/check_reachability.py
```

It exits 1 only on findings that are NOT already in `reachability-baseline.json`.
Add `--json` for machine-readable output, or `--skip-vulture` if vulture is not
installed locally. See `docs/quality/REACHABILITY-TRIAGE-2026-10.md` for the
triage policy behind each baselined entry.


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

## Definition of Done (BOD-316: "done means wired")

A story is not done when the code merges; it is done when something in
production calls it. Before marking a story Done:

- A production caller exists, or the story is explicitly scoped
  library-only in its ticket (and the ticket says so).
- The live path is demonstrated: a test, demo script, or evidence log shows
  the new code executing on a real call path, not only in an isolated unit
  test.
- No new stub is left as the default. A stub/adapter registered with a
  default-off or default-unavailable health is fine only if an existing
  ticket owns turning it on; otherwise wire it or do not ship it as default.
- Reviewers ask: **"who calls this in production?"** An answer naming only
  `tests/` is not a production caller.
- `scripts/check_reachability.py` (see the Lint CI job) must not report the
  new code as unreachable; see `reachability-baseline.json` for pre-existing
  exceptions and `docs/quality/REACHABILITY-TRIAGE-2026-10.md` for the triage
  policy.

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