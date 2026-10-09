"""Vulture whitelist: intentionally-unused names vulture would otherwise flag.

Run in CI as:
    vulture verdict/ .vulture-whitelist.py --min-confidence 80

Each entry below is a deliberate false positive, not dead code:
- Standard dunder/protocol parameters that must exist for the interface
  contract even though the body does not read them.
- A deliberate re-export (`noqa: F401`) that other modules/tests import
  through this module's name, not through the original definition.
- Public function parameters kept for API/call-site compatibility.

Do NOT add a name here to silence a real dead-code finding -- delete the
dead code instead. Only add a name here after confirming (git grep +
codebase-memory/caller check) that it is a false positive, same as the
repo-quality review's quick-wins pass (BOD-317) did for the real
deletions in this same change.
"""

# verdict/tracing.py: _NoOpSpan.__exit__ -- standard context-manager dunder
# signature; the no-op body intentionally ignores all three arguments.
exc_type
exc_val
exc_tb

# verdict/home.py: VerdictCompleter.get_completions -- prompt_toolkit's
# Completer.get_completions(document, complete_event) interface; the
# second positional argument is required by the base class, unused here.
complete_event

# verdict/orchestration/runtime.py: deliberate re-export so
# `from verdict.orchestration.runtime import MAX_CANDIDATES` keeps working
# for callers/tests that import it from this module rather than
# verdict.orchestration.candidate_builder (see the inline `noqa: F401`).
_MAX_CANDIDATES

# verdict/autodev_run.py: run_packet_attempt() keyword parameter kept for
# call-site/API compatibility with existing callers; not read in the
# current function body.
refresh_fallback

# verdict/orchestration/cli.py: _ExplicitPrefer.__call__ -- argparse.Action
# interface signature (parser, namespace, values, option_string); the
# override must accept option_string even though it does not read it.
option_string
