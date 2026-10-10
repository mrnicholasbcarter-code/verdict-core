
from verdict.classifier import classify


def test_gemini_models() -> None:
    # gemini should not trip the generic mini(?!max) cheap marker
    # Pro variants
    assert classify("gemini-2.5-pro") == 1
    assert classify("gemini-3-pro") == 1
    assert classify("gemini-3.1-pro-high") == 1

    # Flash / Lite variants (cheap)
    assert classify("gemini-2.5-flash") == 3
    assert classify("gemini-3.1-flash-lite") == 3
    assert classify("gemini-nano") == 3

def test_gpt_6_sol_variants() -> None:
    assert classify("gpt-6-sol") == 0
    assert classify("gpt-6.1-sol") == 0
    assert classify("gpt-6.2-sol") == 2 # Unknown defaults to 2 if no other pattern matches, but maybe it doesn't match gpt-6 unless explicit
