"""Test for BOD-196: intelligence_adapter default gate bug"""

import pytest
from verdict.intelligence_adapter import IntelligenceAdapter
from verdict.models import ModelInfo


def test_default_eligibility_gate_can_evaluate():
    """
    Test that the default EligibilityGate created by IntelligenceAdapter
    can actually be used to evaluate candidates.
    
    This test exercises the bug at intelligence_adapter.py:196 where
    availability_source="intelligence-adapter" (a string) is passed
    instead of None or a callable. When the gate tries to call
    availability_source(model_id), it will fail with TypeError.
    """
    # Create adapter with default eligibility_gate (no args)
    adapter = IntelligenceAdapter()
    
    # Create a test candidate
    candidate = ModelInfo(
        provider="openai",
        id="gpt-4",
        capabilities=["chat"],
    )
    
    # This should work but currently fails because availability_source is a string
    # The gate will try to call availability_source(model_id) at eligibility.py:203
    result = adapter.eligibility_gate.evaluate(
        candidates=[candidate],
        protected=False,
        dev_mode=False,
    )
    
    # Should return a result
    assert result is not None
