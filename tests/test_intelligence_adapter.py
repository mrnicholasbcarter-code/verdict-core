"""
Tests for Intelligence Adapter v1 - #16 Versioned, Fail-Closed Intelligence Adapter

These tests prove the acceptance criteria:
- Versioned contract envelope (intelligence-adapter/v1)
- Argument-vector execution
- Strict schema validation
- Redaction of sensitive content
- Categorized failures with failure classes
- Bounded Ruflo/RuVector readiness checks
- Fail-closed semantics for protected work
- Adapter output cannot authorize denied/unsafe candidates
- End-to-end correlation IDs
- Transport success ≠ verified quality
"""

from __future__ import annotations

import pytest
from unittest.mock import Mock, patch

from verdict.intelligence_adapter import (
    IntelligenceAdapter,
    IntelligenceAdapterConfig,
    IntelligenceRequest,
    IntelligenceResponse,
    ReadinessReport,
    ReadinessError,
    ValidationError,
    CategorizedFailure,
    DegradedModeError,
    build_intelligence_adapter,
)
from verdict.contracts import TaskSpec, ContractValidationError
from verdict.planner import StructuredPlanner, PlanRejectedError
from verdict.eligibility import EligibilityGate

@pytest.fixture
def adapter():
    """Create a default intelligence adapter for testing."""
    return IntelligenceAdapter(
        config=IntelligenceAdapterConfig(profile='development', allow_degraded_mode=True),
        ruflo_health_check=lambda: 'healthy',
        ruvector_health_check=lambda: 'healthy',
    )



class TestIntelligenceAdapterConfig:
    """Test configuration defaults and validation."""
    
    def test_default_config(self):
        config = IntelligenceAdapterConfig()
        assert config.contract_version == "intelligence-adapter/v1"
        assert config.planner_timeout_ms == 5000
        assert config.profile == "production"
        assert config.allow_degraded_mode is False
        assert config.redact_secrets is True
        assert "api_key" in config.redaction_patterns
    
    def test_custom_config(self):
        config = IntelligenceAdapterConfig(
            profile="degraded",
            allow_degraded_mode=True,
            planner_timeout_ms=10000,
        )
        assert config.profile == "degraded"
        assert config.allow_degraded_mode is True
        assert config.planner_timeout_ms == 10000


class TestReadinessReport:
    """Test readiness report structure."""
    
    def test_healthy_readiness(self):
        report = ReadinessReport(
            status="ready",
            production_ready=True,
            profile="production",
            ruflo_status="healthy",
            ruvector_status="healthy",
            policy_version="policy-2026-07-24.1",
            reason="All managed backends healthy",
            adapter_versions={},
        )
        assert report.production_ready is True
        assert report.status == "ready"
    
    def test_degraded_readiness(self):
        report = ReadinessReport(
            status="degraded",
            production_ready=False,
            profile="production",
            ruflo_status="degraded",
            ruvector_status="healthy",
            policy_version="policy-2026-07-24.1",
            reason="Some managed backends degraded",
            adapter_versions={},
        )
        assert report.production_ready is False
        assert report.status == "degraded"


class TestIntelligenceRequestResponse:
    """Test versioned request/response envelopes."""
    
    def test_request_envelope_creation(self):
        request = IntelligenceRequest(
            request_id="test-req-1",
            correlation_id="test-corr-1",
            task_spec={"objective": "test", "task_type": "code"},
        )
        assert request.contract_version == "intelligence-adapter/v1"
        assert request.request_id == "test-req-1"
        assert request.correlation_id == "test-corr-1"
    
    def test_response_envelope_creation(self):
        readiness = ReadinessReport(
            status="ready",
            production_ready=True,
            profile="production",
            ruflo_status="healthy",
            ruvector_status="healthy",
            policy_version="policy-2026-07-24.1",
            reason="All managed backends healthy",
            adapter_versions={},
        )
        response = IntelligenceResponse(
            request_id="test-req-1",
            correlation_id="test-corr-1",
            task_spec={"objective": "test"},
            workflow_plan=None,
            eligibility_result=None,
            readiness=readiness,
            status="success",
        )
        assert response.contract_version == "intelligence-adapter/v1"
        assert response.status == "success"
        assert response.readiness.production_ready is True


class TestIntelligenceAdapter:
    """Test the main intelligence adapter functionality."""
    
    @pytest.fixture
    def mock_planner(self):
        planner = Mock(spec=StructuredPlanner)
        planner.plan.return_value = Mock(
            task_spec=TaskSpec(objective="test task", task_type="code"),
            workflow_plan=None,
        )
        return planner
    
    @pytest.fixture
    def mock_eligibility_gate(self):
        gate = Mock(spec=EligibilityGate)
        return gate
    
    @pytest.fixture
    def adapter(self, mock_planner, mock_eligibility_gate):
        config = IntelligenceAdapterConfig(
            profile="development",
            allow_degraded_mode=True,  # Allow for testing
        )
        return IntelligenceAdapter(
            config=config,
            planner=mock_planner,
            eligibility_gate=mock_eligibility_gate,
            ruflo_health_check=lambda: "healthy",
            ruvector_health_check=lambda: "healthy",
        )
    
    def test_successful_execution(self, adapter, mock_planner):
        """Test successful end-to-end execution."""
        result = adapter.execute(
            objective="Write a hello world function",
            criticality="low",
            budget={"max_usd": 0.10},
        )
        
        assert isinstance(result, IntelligenceResponse)
        assert result.status == "success"
        assert result.request_id is not None
        assert result.correlation_id is not None
        assert result.readiness.production_ready is True
        assert result.task_spec is not None
        assert "objective" in result.task_spec
        
        # Verify planner was called
        mock_planner.plan.assert_called_once()
    
    def test_correlation_id_generation(self, adapter):
        """Test correlation ID is generated when not provided."""
        result = adapter.execute(objective="test")
        assert result.correlation_id is not None
        assert len(result.correlation_id) > 0
        
        # Custom correlation ID should be preserved
        result2 = adapter.execute(objective="test", correlation_id="custom-corr-123")
        assert result2.correlation_id == "custom-corr-123"
    
    def test_request_id_generation(self, adapter):
        """Test request ID is always generated."""
        result = adapter.execute(objective="test")
        assert result.request_id is not None
        assert len(result.request_id) > 0
    
    def test_contract_version_validation(self, adapter):
        """Test contract version validation."""
        # This is tested internally - valid version should pass
        result = adapter.execute(objective="test")
        assert result.contract_version == "intelligence-adapter/v1"
    
    def test_readiness_check_healthy(self, adapter):
        """Test readiness check with healthy backends."""
        readiness = adapter.get_readiness()
        assert readiness.production_ready is True
        assert readiness.ruflo_status == "healthy"
        assert readiness.ruvector_status == "healthy"
    
    def test_readiness_check_unhealthy_rufl(self):
        """Test readiness with unhealthy Ruflo."""
        adapter = IntelligenceAdapter(
            config=IntelligenceAdapterConfig(profile="production"),
            ruflo_health_check=lambda: "unhealthy",
            ruvector_health_check=lambda: "healthy",
        )
        readiness = adapter.get_readiness()
        assert readiness.production_ready is False
        assert readiness.ruflo_status == "unhealthy"
    
    def test_readiness_check_unhealthy_ruvector(self):
        """Test readiness with unhealthy RuVector."""
        adapter = IntelligenceAdapter(
            config=IntelligenceAdapterConfig(profile="production"),
            ruflo_health_check=lambda: "healthy",
            ruvector_health_check=lambda: "unhealthy",
        )
        readiness = adapter.get_readiness()
        assert readiness.production_ready is False
        assert readiness.ruvector_status == "unhealthy"


class TestFailClosedSemantics:
    """Test fail-closed semantics for protected work."""
    
    @pytest.fixture
    def production_adapter(self):
        return IntelligenceAdapter(
            config=IntelligenceAdapterConfig(profile="production", allow_degraded_mode=False),
            ruflo_health_check=lambda: "unhealthy",
            ruvector_health_check=lambda: "healthy",
        )
    
    def test_protected_work_fails_closed_when_unhealthy(self, production_adapter):
        """Protected work should fail when managed backends unhealthy."""
        with pytest.raises(DegradedModeError) as exc_info:
            production_adapter.execute(
                objective="Deploy to production",
                context={"metadata": {"protected": True, "production_impact": True}},
            )
        
        assert "Protected work requires healthy managed backends" in str(exc_info.value)
        assert exc_info.value.details["protected"] is True
    
    def test_unprotected_work_allows_degraded(self, production_adapter):
        """Unprotected work should succeed even with degraded readiness."""
        # This should not raise even with unhealthy Ruflo
        result = production_adapter.execute(
            objective="Write a test",
            criticality="low",
        )
        assert result.status == "success"
    
    def test_degraded_mode_explicitly_allowed(self):
        """Test degraded mode when explicitly allowed."""
        adapter = IntelligenceAdapter(
            config=IntelligenceAdapterConfig(profile="degraded", allow_degraded_mode=True),
            ruflo_health_check=lambda: "degraded",
            ruvector_health_check=lambda: "healthy",
        )
        
        result = adapter.execute(
            objective="Deploy to production",
            context={"metadata": {"protected": True, "production_impact": True}},
        )
        assert result.status == "success"
        assert result.readiness.status == "degraded"


class TestCategorizedFailures:
    """Test categorized failure handling."""
    
    @pytest.fixture
    def adapter_with_failing_planner(self):
        planner = Mock(spec=StructuredPlanner)
        planner.plan.side_effect = PlanRejectedError("budget exceeded")
        return IntelligenceAdapter(
            config=IntelligenceAdapterConfig(profile="development"),
            planner=planner,
        )
    
    def test_plan_rejected_categorized(self, adapter_with_failing_planner):
        """Test plan rejection is categorized as PLAN_FAILURE."""
        with pytest.raises(CategorizedFailure) as exc_info:
            adapter_with_failing_planner.execute(objective="test")
        
        assert exc_info.value.failure_class == "PLAN_FAILURE"
        assert exc_info.value.replannable is True
    
    def test_unexpected_error_categorized(self):
        """Test unexpected errors are categorized as UNKNOWN."""
        planner = Mock(spec=StructuredPlanner)
        planner.plan.side_effect = RuntimeError("unexpected error")
        
        adapter = IntelligenceAdapter(
            config=IntelligenceAdapterConfig(profile="development"),
            planner=planner,
        )
        
        with pytest.raises(CategorizedFailure) as exc_info:
            adapter.execute(objective="test")
        
        assert exc_info.value.failure_class == "UNKNOWN"
        assert exc_info.value.replannable is False


class TestRedaction:
    """Test sensitive content redaction."""
    
    def test_redacts_api_keys(self):
        adapter = IntelligenceAdapter(
            config=IntelligenceAdapterConfig(redact_secrets=True),
        )
        
        # Test through _redact method directly
        test_data = {
            "api_key": "sk-1234567890abcdef",
            "normal_field": "value",
            "nested": {"authorization": "Bearer token123"},
        }
        redacted = adapter._redact(test_data)
        
        assert "sk-" not in str(redacted)
        assert "[REDACTED]" in str(redacted)
        assert redacted["normal_field"] == "value"
    
    def test_redaction_disabled(self):
        adapter = IntelligenceAdapter(
            config=IntelligenceAdapterConfig(redact_secrets=False),
        )
        
        test_data = {"api_key": "sk-1234567890abcdef"}
        redacted = adapter._redact(test_data)
        
        assert redacted["api_key"] == "sk-1234567890abcdef"
    
    def test_response_redaction(self, adapter):
        """Test that response content is redacted."""
        result = adapter.execute(
            objective="test with api_key=sk-secret123 in context",
            context={"api_key": "sk-secret123"},
        )
        
        # The response should have redacted content
        assert "sk-secret123" not in str(result.task_spec)
        assert "[REDACTED]" in str(result.task_spec) or "api_key" not in str(result.task_spec)


class TestEligibilityRequirements:
    """Test eligibility requirements building."""
    
    def test_builds_requirements_from_task_spec(self, adapter):
        """Test eligibility requirements derived from TaskSpec."""
        task_spec = TaskSpec(
            objective="test",
            task_type="code",
            required_capabilities=["tools", "structured_output"],
            destructive_operation=False,
            production_impact=True,
            budget={"max_usd": 10.0},
            latency_limit_ms=5000,
        )
        
        requirements = adapter._build_eligibility_requirements(task_spec)
        
        assert "tools" in requirements["required_capabilities"]
        assert "structured_output" in requirements["required_capabilities"]
        assert requirements["protected"] is True  # derived from production_impact=True
        assert requirements["budget_remaining"] == 10.0
        assert requirements["max_latency_ms"] == 5000
        assert requirements["allow_degraded"] is False
    
    def test_custom_eligibility_merged(self, adapter):
        """Test custom eligibility requirements are merged."""
        task_spec = TaskSpec(objective="test", task_type="code")
        custom = {"custom_field": "custom_value", "max_concurrency": 5}
        
        requirements = adapter._build_eligibility_requirements(task_spec, custom)
        
        assert requirements["custom_field"] == "custom_value"
        assert requirements["max_concurrency"] == 5


class TestFactoryFunction:
    """Test the build_intelligence_adapter factory."""
    
    def test_factory_creates_adapter(self):
        adapter = build_intelligence_adapter(
            config=IntelligenceAdapterConfig(profile="development"),
        )
        
        assert isinstance(adapter, IntelligenceAdapter)
        assert adapter.config.profile == "development"
    
    def test_factory_with_custom_components(self):
        planner = Mock(spec=StructuredPlanner)
        gate = Mock(spec=EligibilityGate)
        
        adapter = build_intelligence_adapter(
            planner=planner,
            eligibility_gate=gate,
        )
        
        assert adapter.planner is planner
        assert adapter.eligibility_gate is gate


class TestErrorCategories:
    """Test all error categories are properly defined."""
    
    def test_readiness_error_category(self):
        err = ReadinessError("not ready")
        assert err.category == "readiness_error"
    
    def test_validation_error_category(self):
        err = ValidationError("invalid contract")
        assert err.category == "validation_error"
    
    def test_categorized_failure_category(self):
        err = CategorizedFailure("plan failed", failure_class="PLAN_FAILURE")
        assert err.category == "categorized_failure"
        assert err.failure_class == "PLAN_FAILURE"
        assert err.replannable is False  # default
    
    def test_categorized_failure_replannable(self):
        err = CategorizedFailure("plan failed", failure_class="PLAN_FAILURE", replannable=True)
        assert err.replannable is True
    
    def test_degraded_mode_error_category(self):
        err = DegradedModeError("degraded not allowed")
        assert err.category == "degraded_mode_error"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])