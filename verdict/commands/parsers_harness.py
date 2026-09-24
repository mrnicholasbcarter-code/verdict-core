"""Argument registration for Verdict harness command family."""

from __future__ import annotations

from typing import Any

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


def register(subparsers: Any) -> None:
    harness_p = subparsers.add_parser(
        "harness", help="Point a coding-agent harness at Verdict without hand-editing its config"
    )
    harness_sub = harness_p.add_subparsers(dest="harness_target", required=True)
    harness_codex_p = harness_sub.add_parser(
        "codex", help="Enable, disable, or inspect Codex as a Verdict OpenAI-compatible client"
    )
    harness_codex_sub = harness_codex_p.add_subparsers(dest="harness_codex_command", required=True)
    harness_enable_p = harness_codex_sub.add_parser(
        "enable", help="Backup ~/.codex/config.toml and set model_provider = verdict"
    )
    harness_enable_p.add_argument(
        "--base-url",
        default=CODEX_HARNESS_DEFAULT_BASE_URL,
        help="Verdict OpenAI-compatible base URL (default: http://127.0.0.1:8000/v1)",
    )
    harness_enable_p.add_argument(
        "--token-env",
        default=CODEX_HARNESS_DEFAULT_TOKEN_ENV,
        help="Env var Codex should read for the bearer token (default: LLMGATE_AUTH_TOKEN; never printed)",
    )
    harness_enable_p.add_argument(
        "--force",
        action="store_true",
        help="Write Codex config even if the Verdict health check fails",
    )
    harness_codex_sub.add_parser(
        "disable", help="Restore the pre-enable ~/.codex/config.toml backup"
    )
    harness_codex_sub.add_parser(
        "status", help="Show active Codex provider, base URL, and whether the token env is set"
    )
    harness_hermes_p = harness_sub.add_parser(
        "hermes", help="Enable, disable, or inspect Hermes as a Verdict OpenAI-compatible client"
    )
    harness_hermes_sub = harness_hermes_p.add_subparsers(
        dest="harness_hermes_command", required=True
    )
    hermes_enable_p = harness_hermes_sub.add_parser(
        "enable", help="Backup ~/.hermes/config.yaml and point model.provider at Verdict"
    )
    hermes_enable_p.add_argument(
        "--base-url",
        default=HERMES_HARNESS_DEFAULT_BASE_URL,
        help="Verdict OpenAI-compatible base URL (default: http://127.0.0.1:8000/v1)",
    )
    hermes_enable_p.add_argument(
        "--token-env",
        default=HERMES_HARNESS_DEFAULT_TOKEN_ENV,
        help="Env var Hermes should read for the bearer token (default: LLMGATE_AUTH_TOKEN)",
    )
    hermes_enable_p.add_argument(
        "--model",
        default=HERMES_HARNESS_DEFAULT_MODEL,
        help="Default model id to set under model.default",
    )
    hermes_enable_p.add_argument(
        "--force",
        action="store_true",
        help="Write Hermes config even if the Verdict health check fails",
    )
    harness_hermes_sub.add_parser(
        "disable", help="Restore the pre-enable ~/.hermes/config.yaml backup"
    )
    harness_hermes_sub.add_parser(
        "status", help="Show active Hermes provider, base URL, and whether the token env is set"
    )
    harness_claude_p = harness_sub.add_parser(
        "claude", help="Enable, disable, discover, or certify Claude Code as a Verdict client"
    )
    harness_claude_sub = harness_claude_p.add_subparsers(
        dest="harness_claude_command", required=True
    )
    harness_claude_sub.add_parser(
        "discover", help="Observe Claude Code install/config without mutating it"
    )
    claude_enable_p = harness_claude_sub.add_parser(
        "enable",
        help="Backup ~/.claude/settings.json and point OpenAI-compatible traffic at Verdict",
    )
    claude_enable_p.add_argument(
        "--base-url",
        default=CLAUDE_HARNESS_DEFAULT_BASE_URL,
        help="Verdict OpenAI-compatible base URL (default: http://127.0.0.1:8000/v1)",
    )
    claude_enable_p.add_argument(
        "--token-env",
        default=CLAUDE_HARNESS_DEFAULT_TOKEN_ENV,
        help="Env var Claude/OpenAI tooling should read for the bearer token (never printed)",
    )
    claude_enable_p.add_argument(
        "--force",
        action="store_true",
        help="Write Claude settings even if the Verdict health check fails",
    )
    harness_claude_sub.add_parser(
        "disable", help="Restore the pre-enable ~/.claude/settings.json backup"
    )
    harness_claude_sub.add_parser(
        "status",
        help="Show Claude Code Verdict base URL, gate hook, and whether the token env is set",
    )
    claude_certify_p = harness_claude_sub.add_parser(
        "certify",
        help="Evidence-only Claude Code certification (partial until BOD-102 Messages proxy)",
    )
    claude_certify_p.add_argument(
        "--force",
        action="store_true",
        help="Treat Verdict health as ok when probing fails (local proof only)",
    )
    harness_cursor_p = harness_sub.add_parser(
        "cursor",
        help="Enable, disable, discover, or certify Cursor as a Verdict OpenAI-compatible client",
    )
    harness_cursor_sub = harness_cursor_p.add_subparsers(
        dest="harness_cursor_command", required=True
    )
    harness_cursor_sub.add_parser(
        "discover", help="Observe Cursor install/config without mutating it"
    )
    cursor_enable_p = harness_cursor_sub.add_parser(
        "enable", help="Write Verdict-managed Cursor provider config aimed at Verdict :8000"
    )
    cursor_enable_p.add_argument(
        "--base-url",
        default=CURSOR_HARNESS_DEFAULT_BASE_URL,
        help="Verdict OpenAI-compatible base URL (default: http://127.0.0.1:8000/v1)",
    )
    cursor_enable_p.add_argument(
        "--token-env",
        default=CURSOR_HARNESS_DEFAULT_TOKEN_ENV,
        help="Env var Cursor/OpenAI tooling should read for the bearer token (never printed)",
    )
    cursor_enable_p.add_argument(
        "--force",
        action="store_true",
        help="Write Cursor config even if the Verdict health check fails",
    )
    cursor_enable_p.add_argument(
        "--wrapper",
        action="store_true",
        help="Also install ~/.cursor/bin/cursor-verdict wrapper (last-resort integration)",
    )
    harness_cursor_sub.add_parser(
        "disable", help="Restore pre-enable Cursor provider/settings/wrapper backups"
    )
    harness_cursor_sub.add_parser(
        "status", help="Show Cursor Verdict provider, base URL, and whether the token env is set"
    )
    cursor_certify_p = harness_cursor_sub.add_parser(
        "certify", help="Evidence-only Cursor certification (partial; IDE UI toggle is NEEDS_OWNER)"
    )
    cursor_certify_p.add_argument(
        "--force",
        action="store_true",
        help="Treat Verdict health as ok when probing fails (local proof only)",
    )
    harness_prime_p = harness_sub.add_parser(
        "prime", help="Enable, disable, discover, or certify Prime Agent as a Verdict client"
    )
    harness_prime_sub = harness_prime_p.add_subparsers(dest="harness_prime_command", required=True)
    harness_prime_sub.add_parser(
        "discover", help="Observe Prime Agent install/config without mutating it"
    )
    prime_enable_p = harness_prime_sub.add_parser(
        "enable",
        help="Backup ~/.prime/agent/models.json and upsert Verdict OpenAI-compatible provider",
    )
    prime_enable_p.add_argument(
        "--base-url",
        default=PRIME_HARNESS_DEFAULT_BASE_URL,
        help="Verdict OpenAI-compatible base URL (default: http://127.0.0.1:8000/v1)",
    )
    prime_enable_p.add_argument(
        "--token-env",
        default=PRIME_HARNESS_DEFAULT_TOKEN_ENV,
        help="Env var name stored as apiKey (never prints the value)",
    )
    prime_enable_p.add_argument(
        "--force",
        action="store_true",
        help="Write Prime models.json even if the Verdict health check fails",
    )
    harness_prime_sub.add_parser(
        "disable", help="Restore the pre-enable ~/.prime/agent/models.json backup"
    )
    harness_prime_sub.add_parser(
        "status",
        help="Show Prime Agent Verdict provider, base URL, and whether the token env is set",
    )
    prime_certify_p = harness_prime_sub.add_parser(
        "certify",
        help="Evidence-only Prime Agent certification (partial; not-installed when binary missing)",
    )
    prime_certify_p.add_argument(
        "--force",
        action="store_true",
        help="Treat Verdict health as ok when probing fails (local proof only)",
    )
    harness_opencode_p = harness_sub.add_parser(
        "opencode",
        help="Enable, disable, discover, or certify OpenCode as a Verdict OpenAI-compatible client",
    )
    harness_opencode_sub = harness_opencode_p.add_subparsers(
        dest="harness_opencode_command", required=True
    )
    harness_opencode_sub.add_parser(
        "discover", help="Observe OpenCode install/config without mutating it"
    )
    opencode_enable_p = harness_opencode_sub.add_parser(
        "enable",
        help="Backup ~/.config/opencode/opencode.json and upsert Verdict OpenAI-compatible provider",
    )
    opencode_enable_p.add_argument(
        "--base-url",
        default=OPENCODE_HARNESS_DEFAULT_BASE_URL,
        help="Verdict OpenAI-compatible base URL (default: http://127.0.0.1:8000/v1)",
    )
    opencode_enable_p.add_argument(
        "--token-env",
        default=OPENCODE_HARNESS_DEFAULT_TOKEN_ENV,
        help="Env var name recorded for OpenCode auth (never prints the value)",
    )
    opencode_enable_p.add_argument(
        "--force",
        action="store_true",
        help="Write OpenCode config even if the Verdict health check fails",
    )
    harness_opencode_sub.add_parser(
        "disable", help="Restore the pre-enable ~/.config/opencode/opencode.json backup"
    )
    harness_opencode_sub.add_parser(
        "status", help="Show OpenCode Verdict provider, base URL, and whether the token env is set"
    )
    opencode_certify_p = harness_opencode_sub.add_parser(
        "certify",
        help="Evidence-only OpenCode certification (partial; not-installed when binary missing)",
    )
    opencode_certify_p.add_argument(
        "--force",
        action="store_true",
        help="Treat Verdict health as ok when probing fails (local proof only)",
    )
    harness_cline_p = harness_sub.add_parser(
        "cline",
        help="Discover, enable, disable, status, or certify Cline as a Verdict OpenAI-compatible client",
    )
    harness_cline_sub = harness_cline_p.add_subparsers(dest="harness_cline_command", required=True)
    harness_cline_sub.add_parser(
        "discover", help="Report whether Cline CLI/IDE config is present and where it points"
    )
    cline_enable_p = harness_cline_sub.add_parser(
        "enable", help="Backup Cline config and point OpenAI-compatible base URL at Verdict :8000"
    )
    cline_enable_p.add_argument(
        "--base-url",
        default=CLINE_HARNESS_DEFAULT_BASE_URL,
        help="Verdict OpenAI-compatible base URL (default: http://127.0.0.1:8000/v1)",
    )
    cline_enable_p.add_argument(
        "--token-env",
        default=CLINE_HARNESS_DEFAULT_TOKEN_ENV,
        help="Env var Cline should read for the bearer token (default: LLMGATE_AUTH_TOKEN; never printed)",
    )
    cline_enable_p.add_argument(
        "--force",
        action="store_true",
        help="Write Cline config even if the Verdict health check fails",
    )
    harness_cline_sub.add_parser(
        "disable", help="Restore pre-enable Cline provider/settings/providers.json backups"
    )
    harness_cline_sub.add_parser(
        "status", help="Show Cline install state, base URL, and whether the token env is set"
    )
    cline_certify_p = harness_cline_sub.add_parser(
        "certify", help="Emit Cline harness parity facets (partial while IDE secrets need owner)"
    )
    cline_certify_p.add_argument(
        "--force", action="store_true", help="Treat Verdict health as ok for local certify proof"
    )
