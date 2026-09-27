"""Tests for subscription-aware admission wrapper."""

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock

from verdict.admission_with_subscription import admit_with_subscription
from verdict.cost_ledger import _subscription_reserved, subscription_budgets
from verdict.models import ConnectionIdentity


def test_wrapper_exhaustion_hard_drop():
    """Wrapper should produce hard drop when subscription exhausted."""
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
        {"provider": "openai", "account_id": "acct-123", "isActive": True, "testStatus": "ok"}
    ]

    # Set exhausted subscription budget
    subscription_budgets["openai/acct-123/subscription"] = Decimal("100")
    _subscription_reserved["openai/acct-123/subscription"] = Decimal("100")

    admitted = admit_with_subscription(inventory, connections, None, now=now)

    record = admitted.record_for("openai/acct-123/subscription/gpt-4")
    assert record is not None
    assert record.admitted is False
    assert record.first_failed_stage.value == "AVAILABLE"
    assert record.reason == "exhausted"


def test_wrapper_available():
    """Wrapper should admit when subscription has headroom."""
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
        {"provider": "openai", "account_id": "acct-123", "isActive": True, "testStatus": "ok"}
    ]

    # Set available subscription budget
    subscription_budgets["openai/acct-123/subscription"] = Decimal("100")
    _subscription_reserved["openai/acct-123/subscription"] = Decimal("0")

    admitted = admit_with_subscription(inventory, connections, None, now=now)

    record = admitted.record_for("openai/acct-123/subscription/gpt-4")
    assert record is not None
    assert record.admitted is True
    assert record.first_failed_stage is None


def test_wrapper_unknown_requires_confirmation():
    """Wrapper should admit but mark as unverified when headroom unknown."""
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
        {"provider": "openai", "account_id": "acct-123", "isActive": True, "testStatus": "ok"}
    ]

    # No subscription budget set

    admitted = admit_with_subscription(inventory, connections, None, now=now)

    record = admitted.record_for("openai/acct-123/subscription/gpt-4")
    assert record is not None
    assert record.admitted is True
    assert record.health == "unknown"
    assert record.first_failed_stage is None
    assert record.reason == "admitted_unverified"
