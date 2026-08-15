# Reproducible flagship demo

The public demo is intentionally a **deterministic fixture simulation**, not a live provider call or production-readiness proof. It proves the contract and explainability slice without credentials, network access, a running gateway, or a mutable local decision log.

## Clean-environment quickstart

### Source-checkout path (recommended)

From the repository root (no pre-install required):

```bash
python scripts/flagship_demo.py
```

The wrapper script adds the repository root to `sys.path`, so the local `verdict` package resolves without an explicit install. The output is a deterministic JSON document.

### Editable-install path

If you prefer a full install:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
python scripts/flagship_demo.py
pytest -q tests/test_flagship_demo.py
```

### Wheel smoke path

This path verifies that the packaged artifact imports cleanly in a fresh environment, without assuming an existing editable checkout is active:

```bash
python -m pip install build
python -m build
python -m venv /tmp/verdict-smoke
source /tmp/verdict-smoke/bin/activate
pip install dist/verdict_core-*.whl
python -m verdict.cli quickstart --non-interactive --dry-run
```

### Optional clean-room env scrub

Unset provider variables first if you want explicit evidence that the demo does not depend on them:

```bash
env -u OPENAI_API_KEY -u ANTHROPIC_API_KEY -u LLMGATE_UPSTREAM_API_KEY \
  python scripts/flagship_demo.py
```

The command prints one JSON document. The fixture timestamp, policy version, candidate IDs, and ordering are fixed, so repeated runs produce byte-identical output. It does not read environment variables or write files.

## What the scenario demonstrates

The task asks for `tools` and `structured_output` and is protected. Four fake catalog/runtime rows are evaluated:

| Candidate | Result | Evidence |
|---|---|---|
| `demo/frontier-tools` | eligible and selected | Required capabilities, healthy observation, quota remaining |
| `demo/no-tools` | excluded | Missing `tools` |
| `demo/quota-empty` | excluded | Quota exhausted |
| `demo/unverified` | excluded | Health unknown; protected work cannot use it |

The JSON contains the original `TaskSpec`, normalized requirements, the eligible list, every candidate explanation, and a `RoutingDecisionContract` with the selected route and exclusions. No prompt beyond the fixed fixture objective, credentials, provider responses, or raw runtime payloads are emitted.

## Verify it

```bash
python scripts/flagship_demo.py > /tmp/verdict-demo-1.json
python scripts/flagship_demo.py > /tmp/verdict-demo-2.json
cmp /tmp/verdict-demo-1.json /tmp/verdict-demo-2.json
pytest -q tests/test_flagship_demo.py
```

The test also checks that the selected route and all exclusion reasons remain stable. This is evidence for the deterministic contract/eligibility slice; it is **not evidence of provider quality, end-to-end model execution, or production readiness**.

## Current limitations

- This demo is a **deterministic fixture simulation**, not a live provider or gateway call.
- It is valid evidence for contract shape, candidate exclusion reasons, and reproducibility only.
- It is not evidence of OmniRoute quota truth, upstream health fidelity, end-to-end proxy compatibility, or production readiness.
- The repository's broader release acceptance items still call for separate mock upstream, CLI/client smoke, and filtered OmniRoute verification coverage.
