"""Unified OmniRoute model discovery pipeline (spec v6.1).

Phase a only: the free rule (section 0.2), the provider free-flag fetch with a
48h last-good cache, and the fake-200 body guard. Everything is pure or takes
injected I/O, and the census hook stays dark unless
``VERDICT_PIPELINE_FREE_RULE`` is set (spec flag ``pipeline.free_rule.enabled``).
"""
