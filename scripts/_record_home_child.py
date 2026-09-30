#!/usr/bin/env python3
"""Child process for the home prompt recording.

Runs the real ``run_home()`` interactive path (prompt_toolkit), exactly what a
user gets on a terminal. Only cursor-position requests are disabled, because
the recording pty does not answer them.

``VERDICT_RECORD_PROBE_DELAY`` (seconds, recording only) adds that much latency
to the real gateway probe before it runs, so the startup motion is visible when
the local gateway answers in ~0.1 s. The probe result is still the real one.
The recorder sets it and says so in the cast title.
"""

import os
import sys
import threading

os.environ["PROMPT_TOOLKIT_NO_CPR"] = "1"

import verdict.home as _h

_delay = float(os.environ.get("VERDICT_RECORD_PROBE_DELAY", "0") or 0)
if _delay > 0:
    _real_probe = _h.probe_gateway

    def _slow_probe(*args, **kwargs):
        threading.Event().wait(_delay)
        return _real_probe(*args, **kwargs)

    _h.probe_gateway = _slow_probe

sys.exit(_h.run_home())
