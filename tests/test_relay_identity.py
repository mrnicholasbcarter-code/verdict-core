from verdict.relay import fatal_identity_mismatch


def test_provider_local_model_matches_namespaced_concrete_selection() -> None:
    assert not fatal_identity_mismatch("agy/gemini-3.7-flash-high", "gemini-3.7-flash-high")


def test_different_provider_local_model_still_fails_closed() -> None:
    assert fatal_identity_mismatch("agy/gemini-3.7-flash-high", "gemini-3.7-flash-low")
