from __future__ import annotations

import argparse
import json
from pathlib import Path

from verdict.benchmarking import (
    DEFAULT_FIXTURE_PATH,
    format_benchmark_report,
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
        "--allow-live-provider",
        action="store_true",
        help="Acknowledge that provider measurements are separate and must be explicitly enabled",
    )
    parser.add_argument(
        "--live-provider",
        default=None,
        help="Label for an explicitly enabled live-provider benchmark run",
    )
    args = parser.parse_args()

    report = run_reproducible_benchmarks(
        args.fixture, allow_live_provider=args.allow_live_provider, live_provider=args.live_provider
    )
    print(format_benchmark_report(report), end="")

    if args.output_json:
        output_path = Path(args.output_json)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
