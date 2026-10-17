"""Prime memory-bridge installer signature.

This tree has no Prime branch in ``configure_memory_bridge``. ``pi`` in that
function is Perplexity Pi and stays there. ``install_prime_bridge`` is the
stable no-op lane D replaces. It writes nothing.

``configure_memory_bridge`` calls this for the tool name ``prime`` and does
not add ``prime`` to ``configured_tools``. Remove that call only when this
function writes Prime files and the characterization tests pin those files.
"""

from __future__ import annotations

from pathlib import Path


def install_prime_bridge(home: Path, shared_db: Path) -> None:
    """Accept the stable installer arguments and write nothing."""
    del home, shared_db
