"""Tests for subscription-aware headroom checks."""

import pytest
from unittest.mock import MagicMock
from verdict.models import ProviderConfig, ConnectionIdentity
from verdict.subscription_headroom import (
    check_headroom_subscription,
    subscription_pool_key,
    is_subscription_exhausted,
    subscription_headroom_pct,
)
from decimal import Decimal


def test_subscription_pool_key():
    identity = MagicMock(spec=ConnectionIdentity)
    identity.provider_id = "openai"
    identity.account_id = "acct-123"
    assert subscription_pool_key(identity) == "openai/acct-123/subscription"


def test_check_headroom_subscription_available():
    config = MagicMock(spec=ProviderConfig)
    config.headroom_endpoint = "https://api.example.com/usage"
    identity = MagicMock(spec=ConnectionIdentity)
    identity.provider_id = "openai"
    identity.account_id = "acct-123"
    from verdict.cost_ledger import subscription_budgets, _subscription_reserved
    subscription_budgets["openai/acct-123/subscription"] = Decimal('100')
    _subscription_reserved["openai/acct-123/subscription"] = Decimal('0')
    
    is_avail, pct, reason, meta = check_headroom_subscription("gpt-4", "openai", config, identity)
    assert is_avail is True
    assert pct is not None
    assert reason == "available"
    assert "subscription_remaining_pct" in meta


def test_check_headroom_subscription_exhausted():
    config = MagicMock(spec=ProviderConfig)
    config.headroom_endpoint = "https://api.example.com/usage"
    identity = MagicMock(spec=ConnectionIdentity)
    identity.provider_id = "openai"
    identity.account_id = "acct-123"
    from verdict.cost_ledger import subscription_budgets, _subscription_reserved
    subscription_budgets["openai/acct-123/subscription"] = Decimal('100')
    _subscription_reserved["openai/acct-123/subscription"] = Decimal('100')
    
    is_avail, pct, reason, meta = check_headroom_subscription("gpt-4", "openai", config, identity)
    assert is_avail is False
    assert reason == "exhausted"
    assert "subscription_budget" in meta


def test_check_headroom_subscription_unknown():
    config = MagicMock(spec=ProviderConfig)
    config.headroom_endpoint = None
    identity = MagicMock(spec=ConnectionIdentity)
    is_avail, pct, reason, meta = check_headroom_subscription("gpt-4", "openai", config, identity)
    assert is_avail is False
    assert reason == "unknown"


def test_is_subscription_exhausted():
    from verdict.cost_ledger import subscription_budgets, _subscription_reserved
    key = "test/key"
    subscription_budgets[key] = Decimal('50')
    assert not is_subscription_exhausted(key)
    _subscription_reserved[key] = Decimal('50')
    assert is_subscription_exhausted(key)
    _subscription_reserved[key] = Decimal('51')
    assert is_subscription_exhausted(key)


def test_subscription_headroom_pct_calculation():
    from verdict.cost_ledger import subscription_budgets, _subscription_reserved
    key = "test/key"
    subscription_budgets[key] = Decimal('100')
    assert subscription_headroom_pct(MagicMock(), key) >= 0.0
    _subscription_reserved[key] = Decimal('25')
    assert subscription_headroom_pct(MagicMock(), key) == 75.0
    _subscription_reserved[key] = Decimal('100')
    assert subscription_headroom_pct(MagicMock(), key) == 0.0

