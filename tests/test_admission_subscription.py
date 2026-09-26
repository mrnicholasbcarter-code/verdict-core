"""Test subscription exhaustion hard drop in admission.

Verifies that exhausted subscription pools result in hard drop admission
records with explicit "exhausted" reason and no admitted routes.
"""

import pytest
from unittest.mock import MagicMock, patch
from verdict.admission import admit, AdmissionRecord, AdmittedSet
from verdict.models import ProviderConfig, ConnectionIdentity, Route
from verdict.capacity_models import CapacitySnapshot, PoolStatus
from verdict.cost_ledger import subscription_budgets, _subscription_reserved
from decimal import Decimal
from datetime import datetime, timezone


def test_admission_subscription_exhaustion_hard_drop():
    """When subscription pool is exhausted, admission should hard drop."""
    now = datetime.now(timezone.utc)
    identity = MagicMock(spec=ConnectionIdentity)
    identity.provider_id = "openai"
    identity.account_id = "acct-123"
    identity.workspace_id = "ws-1"
    
    # Mock inventory with subscription route
    inventory = [
        {
            "id": "openai/acct-123/subscription/gpt-4",
            "provider": "openai",
            "owned_by": "openai/acct-123/subscription",
            "capabilities": {"tool_calling": True},
            "max_input_tokens": 4096,
        }
    ]
    
    # Mock connections
    connections = [
        {
            "provider": "openai",
            "account_id": "acct-123",
            "isActive": True,
            "testStatus": "ok",
        }
    ]
    
    # Set up exhausted subscription budget
    from verdict.cost_ledger import subscription_budgets, _subscription_reserved
    subscription_budgs["openai/acct-123/subscription"] = Decimal('100')
    _subscription_reserved["openai/acct-123/subscription"] = Decimal('100')
    
    # Mock headroom endpoint response
    config = MagicMock(spec=ProviderConfig)
    config.headroom_endpoint = "https://api.example.com/usage"
    
    # Patch check_headroom_subscription to return exhausted
    with patch('verdict.subscription_headroom.check_headroom_subscription') as mock_headroom:
        mock_headroom.return_value = (False, 0.0, "exhausted", {})
        
        admitted = admit(
            inventory,
            connections,
            None,
            now=now,
            required_capabilities=frozenset(),
            min_context_tokens=0,
        )
    
    assert admitted is not None
    record = admitted.record_for("openai/acct-123/subscription/gpt-4")
    assert record is not None
    assert record.admitted is False
    assert record.first_failed_stage.value == "AVAILABLE"
    assert record.reason == "exhausted"
    assert len(admitted.records) == 1


def test_admission_subscription_available():
    """When subscription has headroom, admission should allow."""
    now = datetime.now(timezone.utc)
    identity = MagicMock(spec=ConnectionIdentity)
    identity.provider_id = "openai"
    identity.account_id = "acct-123"
    
    inventory = [
        {
            "id": "openai/acct-123/subscription/gpt-4",
            "provider": "openai",
            "owned_by": "openai/acct-123/subscription",
            "capabilities": {"tool_calling": True},
            "max_input_tokens": 4096,
        }
    ]
    
    connections = [
        {
            "provider": "openai",
            "account_id": "acct-123",
            "isActive": True,
            "testStatus": "ok",
        }
    ]
    
    # Set up available subscription budget
    subscription_budgets["openai/acct-123/subscription"] = Decimal('100')
    _subscription_reserved["openai/acct-123/subscription"] = Decimal('0')
    
    # Patch headroom to return available
    with patch('verdict.subscription_headroom.check_headroom_subscription') as mock_headroom:
        mock_headroom.return_value = (True, 50.0, "available", {})
        
        admitted = admit(
            inventory,
            connections,
            None,
            now=now,
            required_capabilities=frozenset(),
            min_context_tokens=0,
        )
    
    record = admitted.record_for("openai/acct-123/subscription/gpt-4")
    assert record is not None
    assert record.admitted is True
    assert record.first_failed_stage is None
    assert record.health == "healthy"


def test_admission_subscription_unknown_requires_confirmation():
    """When headroom is unknown, admission should admit but mark as unverified."""
    now = datetime.now(timezone.utc)
    identity = MagicMock(spec=ConnectionIdentity)
    identity.provider_id = "openai"
    identity.account_id = "acct-123"
    
    inventory = [
        {
            "id": "openai/acct-123/subscription/gpt-4",
            "provider": "openai",
            "owned_by": "openai/acct-123/subscription",
            "capabilities": {"tool_calling": True},
            "max_input_tokens": 4096,
        }
    ]
    
    connections = [
        {
            "provider": "openai",
            "account_id": "acct-123",
            "isActive": True,
            "testStatus": "ok",
        }
    ]
    
    # No subscription budget set
    
    # Patch headroom to return None (unknown)
    with patch('verdict.subscription_headroom.check_headroom_subscription') as mock_headroom:
        mock_headroom.return_value = (False, None, "unknown", {})
        
        admitted = admit(
            inventory,
            connections,
            None,
            now=now,
            required_capabilities=frozenset(),
            min_context_tokens=0,
        )
    
    record = admitted.record_for("openai/acct-123/subscription/gpt-4")
    assert record is not None
    assert record.admitted is True
    assert record.health == "unknown"
    assert record.first_failed_stage is None
    assert record.reason == "admitted_unverified"

