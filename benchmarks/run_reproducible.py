from __future__ import annotations

import argparse
import json
from pathlib import Path

from verdict.benchmarking import (
    DEFAULT_COMPARISON_FIXTURE_PATH,
    DEFAULT_FIXTURE_PATH,
    format_benchmark_report,
    format_comparison_report,
    run_comparison_benchmarks,
    run_reproducible_benchmarks,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run reproducible local verdict benchmarks")
    parser.add_argument(
        "--fixture",
        default=str(DEFAULT_FIXTURE_PATH),
        help="Path to checked-in benchmark fixture JSON",
    )
    parser.add_argument(
        "--output-json", default=None, help="Optional path to write the full JSON report"
    )
    parser.add_argument(
        "--fail-on-threshold",
        action="store_true",
        help="Exit non-zero when any checked-in reproducible threshold fails",
    )
    parser.add_argument(
        "--allow-live-provider",
        action="store_true",
        help="Acknowledge that provider measurements are separate and must be explicitly enabled",
    )
    parser.add_argument(
        "--live-provider",
        default=None,
        help="Label for an explicitly enabled live-provider benchmark run",
    )
    parser.add_argument(
        "--comparison-fixture",
        default=None,
        help=f"Run the offline direct-vs-Verdict fixture (default: {DEFAULT_COMPARISON_FIXTURE_PATH})",
    )
    parser.add_argument(
        "--seed", type=int, default=0, help="Seed deterministic comparison observations"
    )
    parser.add_argument(
        "--fail-on-regression",
        action="store_true",
        help="Exit non-zero when the comparison regression budget fails",
    )
    args = parser.parse_args()

    if args.comparison_fixture:
        report = run_comparison_benchmarks(args.comparison_fixture, seed=args.seed)
        print(format_comparison_report(report), end="")
        if args.output_json:
            output_path = Path(args.output_json)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        if args.fail_on_regression and not report["regression"]["passed"]:
            raise SystemExit(1)
        return

    report = run_reproducible_benchmarks(
        args.fixture, allow_live_provider=args.allow_live_provider, live_provider=args.live_provider
    )
    print(format_benchmark_report(report), end="")

    if args.output_json:
        output_path = Path(args.output_json)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")

    if args.fail_on_threshold and not report["metrics"]["thresholds_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
