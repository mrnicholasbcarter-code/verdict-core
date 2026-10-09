#!/usr/bin/env python3
"""Record the verified-models + /bootstrap demo as an asciinema v2 cast.

Shows, against a FIXTURE health cache (no live calls, isolated HOME/VERDICT_HOME/
PRIME_AGENT_CODING_AGENT_DIR/CLAUDE_HOME):

* ``/eligibility`` verified view with mixed VERIFIED/STALE/FAILED statuses;
* Tab-completion of ``/bootstrap prime``;
* the Prime picker preview, answered N (no mutation);
* ``/bootstrap claude``'s honest read-only compatibility report.

Modeled on ``record_home_prompt.py``: real PTY, real ``verdict.home.run_home()``,
scripted keystrokes (including a literal Tab byte) at fixed delays.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WIDTH, HEIGHT = 110, 44

sys.path.insert(0, str(ROOT / "scripts"))
import record_home_prompt as _rhp  # noqa: E402

CHILD_SCRIPT = (ROOT / "scripts" / "_record_verified_demo_child.py").as_posix()

REQUIRED_MARKERS = (
    "verdict \u203a",
    "VERIFIED",
    "STALE",
    "FAILED",
    "Apply this exact interactive scope?",
)


def recording_problems(chunks: list[tuple[float, bytes]]) -> list[str]:
    text = b"".join(data for _, data in chunks).decode("utf-8", "replace")
    problems = [f"missing {marker!r}" for marker in REQUIRED_MARKERS if marker not in text]
    if "Traceback" in text:
        problems.append("traceback in output")
    for secret_marker in ("API_KEY", "Bearer ", "sk-"):
        if secret_marker in text:
            problems.append(f"secret marker {secret_marker!r} in output")
    return problems


def main() -> int:
    output = ROOT / "docs" / "assets" / "verified-models.cast"
    python = sys.executable

    with tempfile.TemporaryDirectory(prefix="verdict-record-verified-") as home:
        home_path = Path(home)
        verdict_home = home_path / ".verdict"
        prime_dir = home_path / ".prime" / "agent"
        claude_home = home_path / ".claude"
        for d in (verdict_home, prime_dir, claude_home):
            d.mkdir(parents=True, exist_ok=True)

        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(home_path),
            "VERDICT_HOME": str(verdict_home),
            "PRIME_AGENT_CODING_AGENT_DIR": str(prime_dir),
            "CLAUDE_HOME": str(claude_home),
            "PYTHONPATH": str(ROOT),
            "TERM": "xterm-256color",
            "COLORTERM": "truecolor",
            "COLUMNS": str(WIDTH),
            "LINES": str(HEIGHT),
            "PYTHONUNBUFFERED": "1",
            "PROMPT_TOOLKIT_NO_CPR": "1",
            "VERDICT_GATEWAY": "http://127.0.0.1:20128",
            "VERDICT_AUTO_REFRESH": "0",
        }

        inputs = [
            (2.5, "/eligibility\n"),
            (6.0, "/bootstrap pri"),
            (7.5, "\t"),
            (8.5, "\r"),
            (9.0, "\r"),
            (11.0, "cc/sonnet\n"),
            (14.0, "N\n"),
            (16.0, "/bootstrap claude\n"),
            (19.0, "\x04"),
        ]

        argv = [python, CHILD_SCRIPT]
        print(f"Recording verified-models demo into {WIDTH}x{HEIGHT} pty", file=sys.stderr)
        chunks = _rhp.read_pty_with_input(argv, env, inputs, rows=HEIGHT, cols=WIDTH, timeout=30.0)
        if not chunks:
            print("No output captured", file=sys.stderr)
            return 1
        problems = recording_problems(chunks)
        if problems:
            print(f"Recording incomplete: {problems}", file=sys.stderr)
            return 1

        chunks = _rhp.sanitize_chunks(chunks, cwd=os.getcwd())
        body = _rhp.to_asciicast(
            chunks,
            width=WIDTH,
            height=HEIGHT,
            title=(
                "Verdict: offline scenario, scripted fixture evidence (no live calls) -- "
                "VERIFIED eligibility view, Tab completion of /bootstrap prime, a Prime picker "
                "preview answered N, and an honest /bootstrap claude report"
            ),
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(body, encoding="utf-8")
        duration = chunks[-1][0] if chunks else 0
        print(f"wrote {output} ({len(chunks)} frames, {duration:.1f}s)", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
