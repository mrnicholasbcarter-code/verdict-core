"""CLI dispatch. Handlers remain looked up on verdict.cli for patch compatibility."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from verdict.harness_claude import DEFAULT_BASE_URL as CLAUDE_HARNESS_DEFAULT_BASE_URL
from verdict.harness_claude import DEFAULT_TOKEN_ENV as CLAUDE_HARNESS_DEFAULT_TOKEN_ENV
from verdict.harness_cline import DEFAULT_BASE_URL as CLINE_HARNESS_DEFAULT_BASE_URL
from verdict.harness_cline import DEFAULT_TOKEN_ENV as CLINE_HARNESS_DEFAULT_TOKEN_ENV
from verdict.harness_codex import DEFAULT_BASE_URL as CODEX_HARNESS_DEFAULT_BASE_URL
from verdict.harness_codex import DEFAULT_TOKEN_ENV as CODEX_HARNESS_DEFAULT_TOKEN_ENV
from verdict.harness_cursor import DEFAULT_BASE_URL as CURSOR_HARNESS_DEFAULT_BASE_URL
from verdict.harness_cursor import DEFAULT_TOKEN_ENV as CURSOR_HARNESS_DEFAULT_TOKEN_ENV
from verdict.harness_hermes import DEFAULT_BASE_URL as HERMES_HARNESS_DEFAULT_BASE_URL
from verdict.harness_hermes import DEFAULT_MODEL as HERMES_HARNESS_DEFAULT_MODEL
from verdict.harness_hermes import DEFAULT_TOKEN_ENV as HERMES_HARNESS_DEFAULT_TOKEN_ENV
from verdict.harness_opencode import DEFAULT_BASE_URL as OPENCODE_HARNESS_DEFAULT_BASE_URL
from verdict.harness_opencode import DEFAULT_TOKEN_ENV as OPENCODE_HARNESS_DEFAULT_TOKEN_ENV
from verdict.harness_prime import DEFAULT_BASE_URL as PRIME_HARNESS_DEFAULT_BASE_URL
from verdict.harness_prime import DEFAULT_TOKEN_ENV as PRIME_HARNESS_DEFAULT_TOKEN_ENV


def dispatch(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    # Existing integrations monkeypatch verdict.cli.cmd_* and provider helpers.
    # Keep those lookups dynamic until the compatibility contract is migrated.
    from verdict import cli as legacy
    from verdict.orchestration import cli as orchestration_cli

    orchestration_rc = orchestration_cli.dispatch(args)
    if orchestration_rc is not None:
        raise SystemExit(orchestration_rc)

    if args.command == "setup":
        scope = "all"
        if args.setup_action == "credentials":
            legacy.cmd_setup_credentials(non_interactive=args.non_interactive)
            return
        if args.setup_action in {"intelligence", "gateways", "harnesses"}:
            scope = args.setup_action
        if getattr(args, "rollback", False):
            legacy.cmd_setup(
                rollback=True,
                rollback_actions=list(getattr(args, "rollback_actions", None) or []),
                output_json=args.json,
                state_dir=getattr(args, "state_dir", None),
            )
        elif args.setup_action == "plan" or args.plan:
            legacy.cmd_setup_plan(output_json=args.json, scope=scope, recommended=args.recommended)
        elif args.setup_action in {"intelligence", "gateways", "harnesses"} and not args.apply:
            # Bare scoped subcommands plan bootstrap for that scope (not the legacy wizard).
            legacy.cmd_setup_plan(output_json=args.json, scope=scope, recommended=True)
        else:
            legacy.cmd_setup(
                dry_run=args.dry_run,
                output_json=args.json,
                non_interactive=args.non_interactive,
                recommended=args.recommended,
                plan_only=False,
                scope=scope,
                allowlist=list(args.allowlist or []),
                consent=bool(args.yes),
                apply=bool(args.apply),
                state_dir=getattr(args, "state_dir", None),
            )
    elif args.command == "credentials":
        if args.credentials_command == "list":
            legacy.cmd_credentials_list(output_json=args.json)
        elif args.credentials_command == "set":
            legacy.cmd_credentials_set(
                name=args.name, force_unregistered=args.force_unregistered, from_stdin=args.stdin
            )
        elif args.credentials_command == "unset":
            legacy.cmd_credentials_unset(name=args.name)
        elif args.credentials_command == "test":
            legacy.cmd_credentials_test(name=args.name)
        else:
            parser.print_help()
            sys.exit(1)
    elif args.command == "route":
        legacy.cmd_route(
            args.task,
            args.criticality,
            args.terse,
            allow_offline=getattr(args, "allow_offline", False),
            allow_legacy_selector=getattr(args, "allow_legacy_selector", None),
        )
    elif args.command == "autodev":
        if args.autodev_action == "packet":
            if args.packet_action == "shadow":
                legacy.cmd_autodev_packet_shadow(args.episodes, output_json=args.json)
            elif args.packet_action == "canary":
                if args.rollback:
                    legacy.cmd_autodev_packet_canary_rollback(args.rollback, output_json=args.json)
                else:
                    legacy.cmd_autodev_packet_canary(
                        args.episodes, args.admitted, output_json=args.json
                    )
            elif args.packet_action == "execute":
                decision = legacy._cli_execution_path_decision(
                    parser, getattr(args, "execution_path_request", None), packet_path=args.packet
                )
                legacy.cmd_autodev_packet_execute(
                    args.packet,
                    getattr(args, "repo", "."),
                    output_json=args.json,
                    allow_live=getattr(args, "allow_live", False),
                    resume=True,
                    primary_fallback=getattr(args, "primary_fallback", None),
                    prefer_non_primary=getattr(args, "prefer_non_primary", False),
                    base_url=getattr(args, "packet_base_url", None),
                    canary_path=getattr(args, "canary_path", None),
                    delegation=getattr(args, "delegation", None),
                    undelegable_reason=getattr(args, "undelegable_reason", None),
                    execution_path_decision=decision,
                )
            else:
                legacy.cmd_autodev_packet(
                    args.packet_action,
                    args.packet,
                    source_path=getattr(args, "source_path", None),
                    model=getattr(args, "model", None),
                    output_json=args.json,
                    family_a_path=getattr(args, "family_a_path", None),
                    family_b_path=getattr(args, "family_b_path", None),
                )
        else:
            if not args.objective:
                parser.error("verdict autodev requires --objective unless using packet operations")
            legacy.cmd_autodev(
                args.objective,
                args.repo,
                orchestrator_model=args.orchestrator_model,
                executor_model=args.executor_model,
                base_url=args.base_url,
                output_json=args.json,
                allow_live=args.allow_live,
                no_mechanical=args.no_mechanical,
                dry_run=args.dry_run,
                execution_path_decision=legacy._cli_execution_path_decision(
                    parser, getattr(args, "execution_path_request", None), task=args.objective
                ),
            )
    elif args.command == "autodev-golden-path":
        legacy.cmd_autodev_golden_path(
            args.objective,
            args.repo,
            args.memory_path,
            args.verify,
            args.timeout,
            args.owned_path,
            args.json,
        )
    elif args.command == "stats":
        legacy.cmd_stats(args.log_path)
    elif args.command == "benchmark":
        legacy.cmd_benchmark(
            args.fixture,
            args.output_json,
            allow_live_provider=args.allow_live_provider,
            live_provider=args.live_provider,
            savings=args.savings,
            live_paired=args.live_paired,
        )
    elif args.command == "quickstart":
        legacy.cmd_quickstart(
            output_json=args.json, non_interactive=args.non_interactive, dry_run=args.dry_run
        )
    elif args.command == "ui":
        try:
            # Resolve the path dynamically without executing the file
            import importlib.util
            import subprocess

            spec = importlib.util.find_spec("verdict.dashboard")
            if not spec or not spec.origin:
                from verdict import present

                present.fail("dashboard", "module missing")
                sys.exit(1)
            subprocess.run([sys.executable, "-m", "streamlit", "run", spec.origin])

        except ImportError:
            from verdict import present

            present.fail("dashboard", "UI dependencies not found")
            present.note('Install them with: pipx install "verdict-core[all]" --force')
            sys.exit(1)
    elif args.command == "serve":
        # Load credentials from store (exported env vars win)
        from verdict.credentials_store import CredentialsStore

        try:
            CredentialsStore().load_into_env()
        except PermissionError as e:
            from verdict import present

            present.fail("credentials", str(e))
            sys.exit(1)

        try:
            from verdict.api import start_server

            if args.dev:
                os.environ["LLMGATE_AVAILABILITY_PROFILE"] = "development"
                from verdict import present

                present.status(
                    "dev mode",
                    "enabled",
                    "hot-reload enabled (LLMGATE_AVAILABILITY_PROFILE=development)",
                )
            start_server(args.port, args.host, reload=args.dev)
        except ImportError:
            from verdict import present

            present.fail("server", "FastAPI dependencies not found")
            present.note('Install them with: pipx install "verdict-core[all]" --force')
            sys.exit(1)
    elif args.command == "probe":
        legacy.cmd_probe(
            args.models,
            base_url=args.base_url,
            timeout=args.timeout,
            output_json=args.json,
            allow_live_probe=args.allow_live_probe,
        )
    elif args.command == "catalog":
        legacy.cmd_catalog(
            base_url=args.base_url,
            management=args.management,
            expected_rows=args.expected_rows,
            freshness_seconds=args.freshness_seconds,
            db_path=args.db_path,
            probe=args.probe,
            probe_limit=args.probe_limit,
            probe_timeout=args.probe_timeout,
            output_json=args.json,
            allow_live_probe=args.allow_live_probe,
        )
    elif args.command == "detect":
        legacy.cmd_detect(
            verbose=args.verbose,
            output_json=args.json,
            output_config=args.config,
            offline=args.offline,
        )
    elif args.command == "certify":
        legacy.cmd_certify(
            snapshot_path=getattr(args, "certify_from", None),
            output_json=getattr(args, "json", True),
        )
    elif args.command == "suggest":
        legacy.cmd_suggest(args.log_path)
    elif args.command == "doctor":
        legacy.cmd_doctor(fix=getattr(args, "fix", False), output_json=getattr(args, "json", False))
    elif args.command == "harness":
        if args.harness_target == "codex":
            legacy.cmd_harness_codex(
                args.harness_codex_command,
                base_url=getattr(args, "base_url", CODEX_HARNESS_DEFAULT_BASE_URL),
                token_env=getattr(args, "token_env", CODEX_HARNESS_DEFAULT_TOKEN_ENV),
                force=getattr(args, "force", False),
            )
        elif args.harness_target == "hermes":
            legacy.cmd_harness_hermes(
                args.harness_hermes_command,
                base_url=getattr(args, "base_url", HERMES_HARNESS_DEFAULT_BASE_URL),
                token_env=getattr(args, "token_env", HERMES_HARNESS_DEFAULT_TOKEN_ENV),
                model=getattr(args, "model", HERMES_HARNESS_DEFAULT_MODEL),
                force=getattr(args, "force", False),
            )
        elif args.harness_target == "claude":
            legacy.cmd_harness_claude(
                args.harness_claude_command,
                base_url=getattr(args, "base_url", CLAUDE_HARNESS_DEFAULT_BASE_URL),
                token_env=getattr(args, "token_env", CLAUDE_HARNESS_DEFAULT_TOKEN_ENV),
                force=getattr(args, "force", False),
            )
        elif args.harness_target == "cursor":
            legacy.cmd_harness_cursor(
                args.harness_cursor_command,
                base_url=getattr(args, "base_url", CURSOR_HARNESS_DEFAULT_BASE_URL),
                token_env=getattr(args, "token_env", CURSOR_HARNESS_DEFAULT_TOKEN_ENV),
                force=getattr(args, "force", False),
                wrapper=getattr(args, "wrapper", False),
            )
        elif args.harness_target == "prime":
            legacy.cmd_harness_prime(
                args.harness_prime_command,
                base_url=getattr(args, "base_url", PRIME_HARNESS_DEFAULT_BASE_URL),
                token_env=getattr(args, "token_env", PRIME_HARNESS_DEFAULT_TOKEN_ENV),
                force=getattr(args, "force", False),
            )
        elif args.harness_target == "opencode":
            legacy.cmd_harness_opencode(
                args.harness_opencode_command,
                base_url=getattr(args, "base_url", OPENCODE_HARNESS_DEFAULT_BASE_URL),
                token_env=getattr(args, "token_env", OPENCODE_HARNESS_DEFAULT_TOKEN_ENV),
                force=getattr(args, "force", False),
            )
        elif args.harness_target == "cline":
            legacy.cmd_harness_cline(
                args.harness_cline_command,
                base_url=getattr(args, "base_url", CLINE_HARNESS_DEFAULT_BASE_URL),
                token_env=getattr(args, "token_env", CLINE_HARNESS_DEFAULT_TOKEN_ENV),
                force=getattr(args, "force", False),
            )
        else:
            raise SystemExit(f"unknown harness: {args.harness_target}")
    elif args.command == "runtime":
        legacy.cmd_runtime(
            args.runtime_command,
            apply=getattr(args, "apply", False),
            consent=getattr(args, "yes", False),
            service_ids=getattr(args, "service_ids", None),
            output_json=getattr(args, "json", False),
        )
    elif args.command == "prove-at-rest":
        legacy.cmd_prove_at_rest(
            args.prove_command,
            base_url=getattr(args, "base_url", None),
            state_path=getattr(args, "state_path", None),
            interval=getattr(args, "interval", 300.0),
            timeout=getattr(args, "timeout", 15.0),
            allow_live_probe=getattr(args, "allow_live_probe", False),
            output_json=getattr(args, "json", False),
        )
    elif args.command == "uninstall":
        legacy.cmd_uninstall(purge_data=getattr(args, "purge_data", False))
    elif args.command == "check":
        legacy.cmd_check()
    elif args.command == "compat":
        legacy.cmd_compat(
            getattr(args, "compat_command", None),
            getattr(args, "declared", None),
            getattr(args, "json", False),
        )
    elif args.command == "memory":
        legacy.cmd_memory(args)
    elif args.command == "mcp":
        legacy.cmd_mcp(args)
    elif args.command == "hook":
        legacy.cmd_hook(args)
    elif args.command == "run":
        legacy.cmd_run(args.task, args.criticality, args.terse)
    elif args.command == "plan":
        legacy.cmd_plan(output_json=args.json)
    elif args.command == "choose":
        legacy.cmd_choose(
            task_class=args.task_class,
            requires=args.requires,
            model=args.model,
            candidates_json=args.candidates_json,
            output_json=args.json,
        )
    elif args.command == "models":
        legacy.cmd_models(output_json=args.json)
    elif args.command == "inspect":
        legacy.cmd_inspect(args.model_id, output_json=args.json)
    elif args.command == "receipt":
        legacy.cmd_receipt(
            args.receipt_action,
            receipt_id=getattr(args, "receipt_id", None),
            attempt_id=getattr(args, "attempt_id", None),
            scope=getattr(args, "scope", None),
            db_path=getattr(args, "db_path", None),
            output_json=bool(getattr(args, "json", False)),
        )
    elif args.command == "replay":
        legacy.cmd_replay(args.session_id, output_json=args.json)
    elif args.command == "simulate":
        legacy.cmd_simulate(
            args.task, args.criticality, model_override=args.model_override, output_json=args.json
        )
    elif args.command == "failover-proof":
        legacy.cmd_failover_proof(memory_path=args.memory_path, output_json=args.json)
    elif args.command == "metadata":
        legacy.cmd_metadata(args)
    elif args.command == "cost-report":
        legacy.cmd_cost_report()
    elif args.command == "resume":
        legacy.cmd_resume(
            args.story,
            with_harness=getattr(args, "with_harness", None),
            output_json=args.json,
            repo=Path(args.repo),
            create_if_missing=bool(getattr(args, "create", False)),
        )
    elif args.command == "openspec":
        # OpenSpec lifecycle commands use func-based dispatch
        if hasattr(args, "func"):
            args.func(args)
        else:
            parser.print_help()
    elif args.command is None and legacy._stdout_is_tty() and os.getenv("VERDICT_PLAIN") != "1":
        # Interactive terminals get the Verdict home screen; pipes, CI and tests
        # keep the historical argparse help contract.
        from verdict.home import run_home

        raise SystemExit(run_home())
    else:
        parser.print_help()
