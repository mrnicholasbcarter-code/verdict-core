"""Argument registration for Verdict runtime command family."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def register(subparsers: Any) -> None:
    runtime_p = subparsers.add_parser(
        "runtime", help="Inspect and safely reconcile optional global runtime ownership records"
    )
    runtime_sub = runtime_p.add_subparsers(dest="runtime_command", required=True)
    runtime_status_p = runtime_sub.add_parser("status", help="Report runtime ownership status")
    runtime_status_p.add_argument("--json", action="store_true", help="Output JSON")
    runtime_explain_p = runtime_sub.add_parser(
        "explain", help="Report observed runtime capability and health evidence"
    )
    runtime_explain_p.add_argument("--json", action="store_true", help="Output JSON")
    runtime_reconcile_p = runtime_sub.add_parser(
        "reconcile", help="Plan or explicitly apply duplicate-service reconciliation"
    )
    runtime_reconcile_p.add_argument(
        "--plan",
        action="store_true",
        help="Perform a read-only deterministic plan (the default when --apply is absent)",
    )
    runtime_reconcile_p.add_argument("--apply", action="store_true", help="Apply planned stops")
    runtime_reconcile_p.add_argument(
        "--yes", action="store_true", help="Explicit consent required with --apply"
    )
    runtime_reconcile_p.add_argument(
        "--service",
        dest="service_ids",
        action="append",
        help="Limit apply to this exact service id; repeat for multiple services",
    )
    runtime_reconcile_p.add_argument("--json", action="store_true", help="Output JSON")

    prove_p = subparsers.add_parser(
        "prove-at-rest", help="Prove free-tier ∩ active OmniRoute models at rest (daemon or once)"
    )
    prove_sub = prove_p.add_subparsers(dest="prove_command", required=True)
    prove_once_p = prove_sub.add_parser("once", help="Run one prove-at-rest cycle and exit")
    prove_daemon_p = prove_sub.add_parser(
        "daemon", help="Continuously prove free∩active identities at rest"
    )
    prove_status_p = prove_sub.add_parser("status", help="Show the latest persisted proof state")
    for _prove_p in (prove_once_p, prove_daemon_p, prove_status_p):
        _prove_p.add_argument(
            "--state-path",
            default=None,
            help="Proof state JSON path (default: ~/.verdict/prove-at-rest/state.json)",
        )
        _prove_p.add_argument("--json", action="store_true", help="Output JSON")
    for _prove_live_p in (prove_once_p, prove_daemon_p):
        _prove_live_p.add_argument(
            "--base-url", default=None, help="OmniRoute origin (default: OMNIROUTE_BASE_URL)"
        )
        _prove_live_p.add_argument(
            "--interval",
            type=float,
            default=300.0,
            help="Daemon interval seconds between cycles (daemon only; default 300)",
        )
        _prove_live_p.add_argument(
            "--timeout", type=float, default=15.0, help="Per-identity probe timeout seconds"
        )
        _prove_live_p.add_argument(
            "--allow-live-probe",
            action="store_true",
            help="Explicit consent to network prove-at-rest probes",
        )
    uninst_p = subparsers.add_parser(
        "uninstall", help="Reversibly uninstall Verdict memory bridge hooks and MCP registrations"
    )
    uninst_p.add_argument(
        "--purge-data", action="store_true", help="Purge .verdict memory database directory"
    )
    subparsers.add_parser("check", help="Validate system configuration file syntax and sanity")

    compat_p = subparsers.add_parser(
        "compat", help="Cross-repo contract compatibility manifest and gate (ADR-024)"
    )
    compat_sub = compat_p.add_subparsers(dest="compat_command")
    compat_manifest_p = compat_sub.add_parser(
        "manifest", help="Print the current verdict-core contract compatibility manifest"
    )
    compat_manifest_p.add_argument("--json", action="store_true", help="Output JSON")
    compat_check_p = compat_sub.add_parser(
        "check",
        help="Check a downstream repo's declared manifest against the current one, failing closed",
    )
    compat_check_p.add_argument(
        "--declared", required=True, help="Path to the downstream repo's declared manifest JSON"
    )
    compat_check_p.add_argument("--json", action="store_true", help="Output JSON")

    memory_p = subparsers.add_parser("memory", help="Local-first unified memory management")
    memory_sub = memory_p.add_subparsers(dest="memory_command")

    put_p = memory_sub.add_parser("put", help="Put a record into memory")
    put_p.add_argument("key", help="Key for memory record")
    put_p.add_argument("content", help="Content of memory record")
    put_p.add_argument("--namespace", default="default", help="Namespace")
    put_p.add_argument("--source", default="cli", help="Source provenance")

    srch_p = memory_sub.add_parser("search", help="Search memory records")
    srch_p.add_argument("query", help="Query text")
    srch_p.add_argument("--namespace", default=None, help="Namespace filter")
    srch_p.add_argument("--limit", type=int, default=10, help="Max results")

    exp_p = memory_sub.add_parser("export", help="Export memory manifest")
    exp_p.add_argument("--output", default="memory_manifest.json", help="Output file")

    imp_p = memory_sub.add_parser("import", help="Import memory manifest")
    imp_p.add_argument("manifest", help="Manifest JSON file")

    md_p = memory_sub.add_parser("masterdocs", help="Canonicalize MasterDocs database")
    md_p.add_argument("--db", default="MasterDocsRAG.db", help="Database path")
    md_p.add_argument(
        "--allow-legacy-sqlite",
        action="store_true",
        help="Explicitly allow an exported local SQLite artifact (prefer manifests)",
    )
    md_p.add_argument("--dry-run", action="store_true", help="Canonicalize without writing memory")
    md_p.add_argument("--limit", type=int, default=1000, help="Maximum source rows to inspect")
    md_p.add_argument(
        "--ingest-timestamp",
        type=float,
        default=None,
        help="Stable provenance timestamp (defaults to deterministic zero)",
    )
    md_p.add_argument("--json", action="store_true", help="Output machine-readable JSON")

    cg_p = memory_sub.add_parser("graph", help="Ingest code review graph database")
    cg_p.add_argument("--db", default="code_graph.db", help="Database path")
    cg_p.add_argument(
        "--allow-legacy-sqlite",
        action="store_true",
        help="Explicitly allow an exported local SQLite artifact (prefer manifests)",
    )

    docs_p = memory_sub.add_parser(
        "docs",
        help="Verify or ingest authoritative project and optional external runtime documentation",
    )
    docs_p.add_argument(
        "--fix", action="store_true", help="Fetch and ingest missing/stale documents"
    )
    docs_p.add_argument("--repo-root", default=str(Path.cwd()), help="Repository root")
    docs_p.add_argument("--db-path", default=None, help="Shared memory database path")
    docs_p.add_argument("--json", action="store_true", help="Output machine-readable JSON")

    setup_p = memory_sub.add_parser(
        "setup", help="Autopilot wizard to connect tools (Codex, Claude, Pi) to Unified Memory"
    )
    setup_p.add_argument(
        "--tools", default=None, help="Comma-separated tools to configure (default: auto-detected)"
    )

    mcp_p = subparsers.add_parser(
        "mcp", help="Manage and run Model Context Protocol (MCP) stdio server"
    )
    mcp_sub = mcp_p.add_subparsers(dest="mcp_command", required=True)
    mcp_sub.add_parser("serve", help="Launch the stdio MCP JSON-RPC server")
    mcp_init_p = mcp_sub.add_parser(
        "init", help="Configure Verdict MCP server across host tool environments"
    )
    mcp_init_p.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    mcp_status_p = mcp_sub.add_parser("status", help="Report active MCP registrations")
    mcp_status_p.add_argument("--json", action="store_true", help="Output machine-readable JSON")

    hook_p = subparsers.add_parser(
        "hook", help="Manage Verdict lifecycle hooks for Codex and Claude"
    )
    hook_sub = hook_p.add_subparsers(dest="hook_command", required=True)
    hook_recall_p = hook_sub.add_parser("recall", help="Search memory for prior context")
    hook_recall_p.add_argument("query", help="Search query")
    hook_recall_p.add_argument("--limit", type=int, default=5, help="Max results")
    hook_recall_p.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    hook_record_p = hook_sub.add_parser("record", help="Record a session/event memory entry")
    hook_record_p.add_argument("key", help="Memory key")
    hook_record_p.add_argument("value", help="Memory value/content")
    hook_record_p.add_argument("--namespace", default="sessions", help="Namespace")
    hook_record_p.add_argument("--source", default="cli", help="Source provenance")
    hook_configure_p = hook_sub.add_parser(
        "configure", help="Configure memory bridge for Codex and Claude"
    )
    hook_configure_p.add_argument(
        "--tools", default=None, help="Comma-separated tools (default: codex,claude)"
    )
    hook_configure_p.add_argument(
        "--json", action="store_true", help="Output machine-readable JSON"
    )
    hook_status_p = hook_sub.add_parser("status", help="Show hook and MCP registration status")
    hook_status_p.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    hook_status_p.add_argument("--db-path", default=None, help="Shared memory database path")
    hook_gate_p = hook_sub.add_parser(
        "claude-gate",
        help="Fail-closed catalog check for Claude Code / Codex SessionStart hooks (exit 2 if blocked)",
    )
    hook_gate_p.add_argument(
        "--base-url",
        default="http://127.0.0.1:20128",
        help="OmniRoute or OpenAI-compatible gateway base URL",
    )
