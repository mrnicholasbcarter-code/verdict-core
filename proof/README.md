# BOD-89 — Canonical local / agent / CI proof pipeline

## Entrypoint (same DAG everywhere)

```bash
# Story worktree — targeted tests first
python scripts/proof/run.py --mode targeted --targets tests/test_foo.py

# Before PR / merge — full required regression (clean final proof)
python scripts/proof/run.py --mode full --no-cache --report .verdict/proof-report.json

# LLM/tool boundary stories only
python scripts/proof/run.py --mode full --llm-boundary --otel
```

GitHub Actions (`.github/workflows/proof.yml`) invokes the **same** command.

## Why native runner (not Dagger)

Dagger was evaluated as a candidate. This repo already pins tool versions via
`uv.lock` / `package-lock.json` and has mature native gates (ruff, mypy, pytest,
bandit, secrets scan, build). A stdlib Python DAG runner:

- preserves **raw exit status** from each gate
- emits **machine-readable failures** for CI-fixer hydration
- avoids a second CI runtime / mandatory paid SaaS
- keeps BOD-89 as the proof engine beneath BOD-68, not a divergent CI system

The contract field `runner: native` documents this choice. A future Dagger
adapter may implement the same contract without changing the entrypoint.

## Contract

`proof/contract.yaml` defines modes, phases, and gate commands. Gates classify
as format / lint / type / schema / test / security / secret / build /
acceptance / structural / adversarial.

| Concern | Gate |
| --- | --- |
| Semgrep CE | `security_semgrep` (required in full) |
| Secrets (local-only) | `secrets` via `scripts/proof/secrets_scan.py` |
| ast-grep | `ast_grep` (optional if binary present) |
| Promptfoo | `promptfoo` only with `--llm-boundary` |
| OTel metadata | `--otel` adds compatible spans to the report |

## Machine-readable failure shape

```json
{
  "gate": "lint",
  "command": ["ruff", "check", "."],
  "file": "tests/test_example.py",
  "test": "test_broken",
  "exit_code": 7,
  "log_slice": "...",
  "classify": "lint"
}
```

## BOD-68 consumption

BOD-68 (PR/CI/merge automation) should:

1. Run `python scripts/proof/run.py --mode targeted ...` during LOCAL_VALIDATION
2. Run `python scripts/proof/run.py --mode full --no-cache --report <path>` before
   PR open / merge
3. Hydrate CI-fixer from `failures[]` (bounded log slices), not full CI logs
4. Treat `ok: false` or non-zero exit as proof incomplete — do not open PRs
5. Never invent a parallel gate list; extend `proof/contract.yaml` instead
