"""Argument registration for Verdict autodev command family."""

from __future__ import annotations

from typing import Any


def register(subparsers: Any) -> None:
    golden_p = subparsers.add_parser(
        "autodev-golden-path",
        help="Run offline discovery, durable memory, and bounded verification",
    )
    golden_p.add_argument("--objective", required=True, help="Bounded mission objective")
    golden_p.add_argument("--repo", required=True, help="Real Git repository to inspect")
    golden_p.add_argument("--memory-path", default=".verdict-golden-memory.db")
    golden_p.add_argument("--verify", nargs="+", default=["git", "status", "--short"])
    golden_p.add_argument("--owned-path", action="append", default=[])
    golden_p.add_argument("--timeout", type=float, default=10.0)
    golden_p.add_argument("--json", action="store_true")

    stats_p = subparsers.add_parser("stats", help="View routing analytics")
    stats_p.add_argument("--log_path", default="verdict-decisions.jsonl")

    benchmark_p = subparsers.add_parser(
        "benchmark", help="Run the reproducible local benchmark harness"
    )
    benchmark_p.add_argument("--fixture", default="benchmarks/fixtures/reproducible.json")
    benchmark_p.add_argument("--output-json", default=None)
    benchmark_p.add_argument("--allow-live-provider", action="store_true")
    benchmark_p.add_argument("--live-provider", default=None)
    benchmark_p.add_argument(
        "--savings",
        action="store_true",
        help=(
            "Run the paired legit-task savings bench (we measure). Without --live-paired "
            "this is a labeled simulation that cannot claim savings."
        ),
    )
    benchmark_p.add_argument(
        "--live-paired",
        action="store_true",
        help=(
            "Execute both arms of every --savings task against OMNIROUTE_BASE_URL "
            "(OMNIROUTE_API_KEY) and bind cost/identity/quality to the execution receipts"
        ),
    )

    quickstart_p = subparsers.add_parser(
        "quickstart", help="Run the credential-free deterministic flagship quickstart"
    )
    quickstart_p.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    quickstart_p.add_argument(
        "--non-interactive", action="store_true", help="Do not prompt for input"
    )
    quickstart_p.add_argument(
        "--dry-run", action="store_true", help="Run the read-only quickstart fixture"
    )

    subparsers.add_parser("ui", help="Launch the Streamlit analytics dashboard")

    serve_p = subparsers.add_parser("serve", help="Launch the FastAPI microservice")
    serve_p.add_argument("--port", type=int, default=8000)
    serve_p.add_argument(
        "--host", default=None, help="Bind address (anonymous mode must be loopback)"
    )
    serve_p.add_argument("--dev", action="store_true", help="Enable hot-reload development mode")

    # New: detect command
    detect_p = subparsers.add_parser("detect", help="Detect available LLM providers")
    detect_p.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    detect_p.add_argument("--json", action="store_true", help="Output JSON")
    detect_p.add_argument(
        "--offline",
        action="store_true",
        help="Deterministic offline mode: no network, no credentials",
    )
    detect_p.add_argument("--config", action="store_true", help="Generate suggested Verdict config")

    certify_p = subparsers.add_parser(
        "certify", help="Emit runtime certification passport JSON (BOD-92 evidence only)"
    )
    certify_p.add_argument(
        "--from",
        dest="certify_from",
        default=None,
        help="Path to DetectedSnapshot JSON fixtures (offline; no live probes)",
    )
    certify_p.add_argument(
        "--json",
        action="store_true",
        default=True,
        help="Output JSON (default; certification is machine-readable)",
    )

    # New: probe command (1-token liveness test before assigning work)
    probe_p = subparsers.add_parser("probe", help="Run a 1-token liveness probe against models")
    probe_p.add_argument(
        "models", nargs="+", help="Model IDs to probe (e.g. openrouter/tencent/hy3:free)"
    )
    probe_p.add_argument(
        "--base-url",
        default="http://localhost:20128/v1",
        help="OpenAI-compatible base URL (default: local OmniRoute)",
    )
    probe_p.add_argument("--timeout", type=float, default=20.0, help="Per-probe timeout seconds")
    probe_p.add_argument(
        "--allow-live-probe",
        action="store_true",
        help="Explicitly consent to network liveness probes",
    )
    probe_p.add_argument("--json", action="store_true", help="Output JSON")

    catalog_p = subparsers.add_parser(
        "catalog", help="Qualify and optionally store a sanitized OmniRoute catalog snapshot"
    )
    catalog_p.add_argument(
        "--base-url", default="http://127.0.0.1:20128", help="OmniRoute base URL"
    )
    catalog_p.add_argument(
        "--management",
        action="store_true",
        help="Use only the documented management endpoint (default fetches both projections)",
    )
    catalog_p.add_argument(
        "--expected-rows",
        type=int,
        default=0,
        help="Exact row count required to qualify (0 = any well-formed non-empty catalog)",
    )
    catalog_p.add_argument("--freshness-seconds", type=int, default=3600)
    catalog_p.add_argument("--db-path", default=None, help="Store qualification in a memory DB")
    catalog_p.add_argument(
        "--probe",
        action="store_true",
        help="Run a bounded liveness sample after catalog qualification",
    )
    catalog_p.add_argument("--probe-limit", type=int, default=16)
    catalog_p.add_argument("--probe-timeout", type=float, default=20.0)
    catalog_p.add_argument(
        "--allow-live-probe",
        action="store_true",
        help="Explicitly consent to network liveness probes",
    )
    catalog_p.add_argument("--json", action="store_true", help="Output machine-readable JSON")

    metadata_p = subparsers.add_parser(
        "metadata", help="Refresh and inspect Core's independent model metadata store (BOD-108)"
    )
    metadata_sub = metadata_p.add_subparsers(dest="metadata_command", required=True)
    metadata_refresh_p = metadata_sub.add_parser(
        "refresh", help="Fetch models.dev + LiteLLM into the on-disk Core store"
    )
    metadata_refresh_p.add_argument(
        "--store",
        dest="store_path",
        default=None,
        help="Store path (default ~/.verdict/model-metadata.json)",
    )
    metadata_refresh_p.add_argument("--mapping", dest="mapping_path", default=None)
    metadata_refresh_p.add_argument(
        "--models-dev-api-file", default=None, help="Offline models.dev api.json fixture"
    )
    metadata_refresh_p.add_argument(
        "--models-dev-models-file", default=None, help="Offline models.dev models.json fixture"
    )
    metadata_refresh_p.add_argument(
        "--litellm-file", default=None, help="Offline LiteLLM JSON fixture"
    )
    metadata_refresh_p.add_argument(
        "--include-p1",
        action="store_true",
        help="Record P1 skip reasons (AA/Arena/OpenLLM/BFCL); scores stored only from fixtures",
    )
    metadata_refresh_p.add_argument(
        "--json", action="store_true", help="Output machine-readable JSON"
    )
    metadata_show_p = metadata_sub.add_parser(
        "show", help="Summarize the on-disk Core metadata store"
    )
    metadata_show_p.add_argument("--store", dest="store_path", default=None)
    metadata_show_p.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    metadata_lookup_p = metadata_sub.add_parser(
        "lookup", help="Look up one OmniRoute id in the Core store (named drop if unmapped)"
    )
    metadata_lookup_p.add_argument("omniroute_id")
    metadata_lookup_p.add_argument("--store", dest="store_path", default=None)
    metadata_lookup_p.add_argument("--mapping", dest="mapping_path", default=None)
    metadata_lookup_p.add_argument(
        "--requires", default="", help="Comma-separated required caps (unknown → named drop)"
    )
    metadata_lookup_p.add_argument(
        "--json", action="store_true", help="Output machine-readable JSON"
    )

    suggest_p = subparsers.add_parser(
        "suggest", help="Review intelligence suggestions from past outcomes"
    )
    suggest_p.add_argument("--log_path", default="verdict-decisions.jsonl")

    doctor_p = subparsers.add_parser(
        "doctor", help="Scan and repair system configuration and connectivity issues"
    )
    doctor_p.add_argument(
        "--fix", action="store_true", help="Automatically repair detected configuration issues"
    )
    doctor_p.add_argument("--json", action="store_true", help="Output machine-readable JSON")
