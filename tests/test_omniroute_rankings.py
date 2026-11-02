import pytest
from verdict.omniroute import fetch_rankings
from verdict.availability import OmniRouteTransportUnsupported, OmniRouteTransportTimeout, OmniRouteTransportMalformed

def test_fetch_rankings_red():
    # This test should fail initially (RED) - it attempts to fetch rankings for 'free-provider' category
    # but we expect it to fail due to network issues or because the endpoint doesn't exist yet
    with pytest.raises(OmniRouteTransportUnsupported):
        fetch_rankings('free-provider', timeout=1)
        
    # Also test timeout case
    with pytest.raises(OmniRouteTransportTimeout):
        fetch_rankings('free-provider', timeout=0.001)
        
    # Test malformed response case
    with pytest.raises(OmniRouteTransportMalformed):
        fetch_rankings('free-provider', timeout=1)
