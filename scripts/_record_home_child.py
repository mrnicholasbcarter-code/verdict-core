#!/usr/bin/env python3
"""Child process for the home prompt recording.

Runs the real ``run_home()`` interactive path (prompt_toolkit), exactly what a
user gets on a terminal. Only cursor-position requests are disabled, because
the recording pty does not answer them.
"""

import os
import sys

os.environ["PROMPT_TOOLKIT_NO_CPR"] = "1"

import verdict.home as _h

sys.exit(_h.run_home())
