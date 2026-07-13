# Contributing to llm-gate

Thanks for your interest. Here's how to contribute.

## Setup

```bash
git clone https://github.com/mrnicholasbcarter-code/llm-gate.git
cd llm-gate
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev,server]'
```

## Running Tests

```bash
.venv/bin/python -m pytest
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy llm_gate --strict
```

## Pull Requests

1. Fork the repo and create a branch from `main`.
2. Add tests for any new functionality.
3. Ensure the project-environment `pytest`, Ruff, and strict mypy commands all pass.
4. Write a clear PR description explaining what and why.

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
- Tests live in `tests/` and mirror the `llm_gate/` structure.

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
