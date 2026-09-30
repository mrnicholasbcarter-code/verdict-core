#!/usr/bin/env python3
"""Child process for the home prompt recording.

Runs run_home() with:
- Fallback input loop (no prompt_toolkit double-render in PTY recordings)
- PROMPT_TOOLKIT_NO_CPR suppressed
"""

import os
import sys

os.environ["PROMPT_TOOLKIT_NO_CPR"] = "1"

import verdict.home as _h

# Force the fallback input loop for clean PTY recording
_original_prompt_toolkit_loop = _h._prompt_toolkit_loop


def _recording_loop(target, tui, state):
    return _h._fallback_input_loop(target, tui, state)


_h._prompt_toolkit_loop = _recording_loop

sys.exit(_h.run_home())
