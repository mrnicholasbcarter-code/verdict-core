"""Per-harness memory-bridge installers.

Lane owners:
- ``claude`` — Claude Code hooks and CLAUDE.md instruction
- ``codex`` — Codex hooks and AGENTS.md instruction
- ``hermes`` — Hermes ``verdict_bridge.json`` when ``~/.hermes`` already exists
- ``prime`` — stable no-op; this tree has no Prime memory-bridge installer

``configure_memory_bridge`` in ``verdict.memory_bridge`` is the production caller.
There is no re-export of the old private ``_install_*`` names: no production
importer used them. Add a re-export only for a real importer, and delete it
when that importer imports these modules directly.
"""
