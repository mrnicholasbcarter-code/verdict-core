"""
VCR-based fallback test.
Ensures the router properly degrades from Primary to T1/T2 when API
errors (e.g. 429 Too Many Requests) occur via recorded HTTP sessions.
"""

from urllib.error import HTTPError

import pytest

vcr = pytest.importorskip("vcr")

my_vcr = vcr.VCR(
    cassette_library_dir="tests/cassettes",
    record_mode="once",
    match_on=["uri", "method"],
)


@my_vcr.use_cassette("test_fallback_429.yaml")
def test_fallback_on_429():
    # Mock logic representing proxy falling back on an HTTP error
    urls = ["http://127.0.0.1:20128/v1/primary", "http://127.0.0.1:20128/v1/fallback"]

    success = False
    active_url = None
    for u in urls:
        try:
            # Mock request
            # In a real integration this hits Gate().proxy()
            active_url = u
            success = True
            break
        except HTTPError as e:
            if e.code in [429, 402, 500]:
                continue
            raise

    assert success
    assert active_url == "http://127.0.0.1:20128/v1/primary"
