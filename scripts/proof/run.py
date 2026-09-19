"""CLI entrypoint for the BOD-89 canonical proof pipeline.

Local and CI invoke the same command:

    python -m scripts.proof.run --mode targeted|full [--no-cache] [--otel] ...
    # or:
    python scripts/proof/run.py --mode targeted|full ...
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Ensure repo root is importable when invoked as a file path.
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.proof.contract import ContractError, load_contract  # noqa: E402
from scripts.proof.runner import ProofRunner, default_executor  # noqa: E402


def _repo_root() -> Path:
    return _ROOT


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Canonical Verdict proof pipeline (local + CI, same DAG)."
    )
    parser.add_argument(
        "--contract",
        type=Path,
        default=_repo_root() / "proof" / "contract.yaml",
        help="Path to proof contract YAML",
    )
    parser.add_argument("--root", type=Path, default=_repo_root(), help="Repository root")
    parser.add_argument(
        "--mode",
        choices=("targeted", "full"),
        default="targeted",
        help="targeted = story gates first; full = required merge regression",
    )
    parser.add_argument(
        "--targets", nargs="*", default=[], help="Targeted test paths (used by unit_targeted gate)"
    )
    parser.add_argument(
        "--no-cache", action="store_true", help="Final proof mode: disable tool caches / bytecode"
    )
    parser.add_argument(
        "--llm-boundary",
        action="store_true",
        help="Enable Promptfoo adversarial gate (LLM/tool boundary stories only)",
    )
    parser.add_argument(
        "--otel",
        action="store_true",
        help="Include OpenTelemetry-compatible span metadata in the report",
    )
    parser.add_argument("--report", type=Path, help="Write machine-readable JSON report")
    parser.add_argument("--json", action="store_true", help="Print JSON report to stdout")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        contract = load_contract(args.contract)
    except ContractError as exc:
        payload = {"ok": False, "error": str(exc), "valid": False}
        print(json.dumps(payload, sort_keys=True), file=sys.stderr)
        return 2

    runner = ProofRunner(
        contract,
        root=args.root,
        executor=lambda argv, *, env, cwd: default_executor(argv, env, cwd),
    )
    report = runner.run(
        mode=args.mode,
        targets=args.targets,
        no_cache=args.no_cache,
        llm_boundary=args.llm_boundary,
        otel=args.otel,
    )
    payload = report.to_dict()
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        status = "PASS" if report.ok else "FAIL"
        print(f"proof {status} mode={report.mode} cache={report.cache_mode} runner={report.runner}")
        for gate in report.gates:
            print(f"  [{gate.status}] {gate.id} exit={gate.exit_code} ({gate.duration_ms}ms)")
        if report.failures:
            print("failures:")
            for failure in report.failures:
                print(
                    f"  - gate={failure['gate']} classify={failure['classify']} "
                    f"exit={failure['exit_code']}"
                )
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
