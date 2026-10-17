"""Pin the installer signatures lanes A-D build against."""

from __future__ import annotations

import inspect
from pathlib import Path

from verdict.memory_bridge import configure_memory_bridge, uninstall_memory_bridge
from verdict.memory_harness.claude import install_claude_hooks, install_claude_instructions
from verdict.memory_harness.codex import install_codex_hooks, install_codex_instructions
from verdict.memory_harness.hermes import install_hermes_bridge
from verdict.memory_harness.prime import install_prime_bridge

MANIFEST = (
    Path(__file__).resolve().parents[1] / "docs/guides/memory-harness-installer-signatures.md"
)

EXPECTED = {
    install_claude_hooks: "(settings_file: 'Path', shared_db: 'Path') -> 'None'",
    install_claude_instructions: "(instructions_file: 'Path', bridge_instruction: 'str') -> 'None'",
    install_codex_hooks: "(hooks_file: 'Path', shared_db: 'Path') -> 'None'",
    install_codex_instructions: "(codex_dir: 'Path', bridge_instruction: 'str') -> 'None'",
    install_hermes_bridge: "(home: 'Path', shared_db: 'Path') -> 'None'",
    install_prime_bridge: "(home: 'Path', shared_db: 'Path') -> 'None'",
    configure_memory_bridge: (
        "(selected_tools: 'list[str]', plane: 'MemoryPlane | None' = None, "
        "home_dir: 'Path | None' = None, cwd: 'Path | None' = None) -> 'dict[str, Any]'"
    ),
    uninstall_memory_bridge: (
        "(home_dir: 'Path', cwd: 'Path', purge_data: 'bool' = False) -> 'dict[str, Any]'"
    ),
}


def test_installer_signatures_match_the_manifest() -> None:
    text = MANIFEST.read_text(encoding="utf-8")
    for function, signature in EXPECTED.items():
        rendered = f"{function.__module__}.{function.__name__}{inspect.signature(function)}"
        assert str(inspect.signature(function)) == signature
        assert rendered in text
