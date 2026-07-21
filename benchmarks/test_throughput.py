"""Backward-compatible entry point for the reproducible local benchmark harness."""

from verdict.benchmarking import format_benchmark_report, run_reproducible_benchmarks


def main() -> None:
    report = run_reproducible_benchmarks()
    print(format_benchmark_report(report), end="")


if __name__ == "__main__":
    main()
