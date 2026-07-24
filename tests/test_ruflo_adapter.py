"""
Tests for Ruflo Adapter Boundary v1 - #38 Protocol/Types for Ruflo Orchestration

These tests prove the acceptance criteria:
- Submit/Status/Pause/Resume/Cancel/Approval/Result typed operations
- Capability manifest for declarative capability requirements
- Fake adapter for deterministic testing (no network credentials required)
- Trust boundaries: Verdict never bypasses Ruflo's authority on protected work
"""

from __future__ import annotations

import pytest
from unittest.mock import Mock

from verdict.ruflo_adapter import (
    RUFLO_ADAPTER_PROTOCOL_VERSION,
    SUPPORTED_PROTOCOL_VERSIONS,
    negotiate_protocol_version,
    RufloAdapter,
    RufloAdapterConfig,
    TaskStatus,
    TaskAction,
    CapabilityManifest,
    RufloSubmitRequest,
    RufloSubmitResponse,
    RufloStatusRequest,
    RufloStatusResponse,
    RufloControlRequest,
    RufloControlResponse,
    RufloResult,
    RufloAdapterError,
    RufloUnavailableError,
    RufloProtocolError,
    RufloCapacityError,
    RufloApprovalError,
    RufloCancellationError,
    RufloVerificationError,
    RufloTimeoutError,
    RufloValidationError,
    build_ruflo_adapter,
    build_fake_ruflo_adapter,
)
from verdict.contracts import TaskSpec, WorkflowPlan, VerificationPlan


class TestProtocolVersion:
    """Test protocol version constants and negotiation."""
    
    def test_protocol_version_constant(self):
        assert RUFLO_ADAPTER_PROTOCOL_VERSION == "rufl-adapter/v1"
    
    def test_supported_versions(self):
        assert "rufl-adapter/v1" in SUPPORTED_PROTOCOL_VERSIONS
    
    def test_negotiate_supported_version(self):
        assert negotiate_protocol_version("rufl-adapter/v1") == "rufl-adapter/v1"
    
    def test_negotiate_unsupported_version_fallback(self):
        result = negotiate_protocol_version("rufl-adapter/v99")
        assert result == "rufl-adapter/v1"


class TestCapabilityManifest:
    """Test capability manifest creation and validation."""
    
    def test_basic_manifest(self):
        manifest = CapabilityManifest(
            required=["task_submission", "status_query"],
            optional=["replan"],
            forbidden=["deprecated_capability"],
        )
        assert manifest.required == ["task_submission", "status_query"]
        assert manifest.optional == ["replan"]
        assert manifest.forbidden == ["deprecated_capability"]
    
    def test_manifest_requires(self):
        manifest = CapabilityManifest(required=["task_submission"])
        assert manifest.requires("task_submission") is True
        assert manifest.requires("unknown") is False
    
    def test_manifest_permits(self):
        manifest = CapabilityManifest(forbidden=["forbidden_cap"])
        assert manifest.permits("allowed") is True
        assert manifest.permits("forbidden_cap") is False
    
    def test_manifest_satisfies(self):
        manifest = CapabilityManifest(required=["a", "b"], optional=["c"])
        assert manifest.satisfies(["a", "b", "c", "d"]) is True
        assert manifest.satisfies(["a", "c"]) is False  # missing b
        assert manifest.satisfies([]) is False
    
    def test_manifest_to_from_dict(self):
        manifest = CapabilityManifest(
            required=["a", "b"],
            optional=["c"],
            forbidden=["d"],
            minimum_versions={"a": "1.0"},
        )
        data = manifest.to_dict()
        restored = CapabilityManifest.from_dict(data)
        assert restored.required == ["a", "b"]
        assert restored.optional == ["c"]
        assert restored.forbidden == ["d"]
        assert restored.minimum_versions == {"a": "1.0"}
    
    def test_manifest_forbidden_required_overlap_raises(self):
        with pytest.raises(ValueError, match="cannot be both required and forbidden"):
            CapabilityManifest(required=["a"], forbidden=["a"])


class TestRequestResponseEnvelopes:
    """Test versioned request/response envelope serialization."""
    
    def test_submit_request_envelope(self):
        task_spec = TaskSpec(objective="test", task_type="code")
        request = RufloSubmitRequest(
            task_spec=task_spec.to_dict(),
            budget_usd=5.0,
            priority=10,
            metadata={"source": "test"},
        )
        
        assert request.contract_version == "rufl-adapter/v1"
        assert request.budget_usd == 5.0
        assert request.priority == 10
        
        data = request.to_dict()
        restored = RufloSubmitRequest.from_dict(data)
        assert restored.task_spec == request.task_spec
        assert restored.budget_usd == 5.0
    
    def test_submit_response_envelope(self):
        response = RufloSubmitResponse(
            task_id="task-123",
            workflow_id="workflow-456",
            status=TaskStatus.QUEUED,
            accepted=True,
            reason="OK",
        )
        
        assert response.contract_version == "rufl-adapter/v1"
        assert response.status == TaskStatus.QUEUED
        
        data = response.to_dict()
        restored = RufloSubmitResponse.from_dict(data)
        assert restored.task_id == "task-123"
        assert restored.status == TaskStatus.QUEUED
    
    def test_status_request_response_envelope(self):
        request = RufloStatusRequest(
            task_id="task-123",
            include_history=True,
            include_verification=True,
        )
        assert request.include_history is True
        
        response = RufloStatusResponse(
            task_id="task-123",
            status=TaskStatus.RUNNING,
            progress_pct=50.0,
            current_step="implementation",
            steps_completed=["research"],
            steps_pending=["test"],
            cost_usd=5.0,
            tokens_used=1000,
        )
        assert response.progress_pct == 50.0
        assert response.current_step == "implementation"
        
        data = response.to_dict()
        restored = RufloStatusResponse.from_dict(data)
        assert restored.progress_pct == 50.0
    
    def test_control_request_response_envelope(self):
        request = RufloControlRequest(
            task_id="task-123",
            action=TaskAction.PAUSE,
            reason="Testing",
        )
        assert request.action == TaskAction.PAUSE
        
        response = RufloControlResponse(
            task_id="task-123",
            action=TaskAction.PAUSE,
            success=True,
            previous_status=TaskStatus.RUNNING,
            new_status=TaskStatus.PAUSED,
        )
        assert response.success is True
        assert response.new_status == TaskStatus.PAUSED
    
    def test_result_envelope(self):
        result = RufloResult(
            task_id="task-123",
            status=TaskStatus.COMPLETED,
            outcome="success",
            output_artifacts=["artifact-1"],
            output_data={"key": "value"},
            verification_passed=True,
            verification_results=[{"check": "tests", "outcome": "pass"}],
            cost_usd=10.0,
            tokens_used=5000,
            latency_ms=30000,
        )
        
        assert result.outcome == "success"
        assert result.verification_passed is True
        
        data = result.to_dict()
        restored = RufloResult.from_dict(data)
        assert restored.task_id == "task-123"
        assert restored.cost_usd == 10.0


class TestFakeAdapter:
    """Test fake adapter for deterministic testing."""
    
    @pytest.fixture
    def fake_adapter(self):
        return build_fake_ruflo_adapter(fake_latency_ms=1)
    
    def test_fake_adapter_creation(self, fake_adapter):
        assert isinstance(fake_adapter, RufloAdapter)
        assert fake_adapter.config.fake_mode is True
        assert fake_adapter.config.fake_latency_ms == 1
    
    def test_fake_submit(self, fake_adapter):
        task_spec = TaskSpec(objective="test task", task_type="code")
        
        response = fake_adapter.submit(task_spec=task_spec)
        
        assert response.accepted is True
        assert response.task_id is not None
        # workflow_id is None when no workflow_plan provided
        assert response.status == TaskStatus.QUEUED
    
    def test_fake_submit_with_workflow(self, fake_adapter):
        task_spec = TaskSpec(objective="test", task_type="code")
        workflow_plan = WorkflowPlan(
            steps=[{"action": "implement"}, {"action": "verify"}],
            verification=VerificationPlan(checks=["tests"]),
        )
        
        response = fake_adapter.submit(
            task_spec=task_spec,
            workflow_plan=workflow_plan,
            budget_usd=10.0,
        )
        
        assert response.workflow_id is not None
        assert response.status == TaskStatus.QUEUED
    
    def test_fake_status_progression(self, fake_adapter):
        task_spec = TaskSpec(objective="test", task_type="code")
        workflow_plan = WorkflowPlan(steps=[{"action": "implement"}, {"action": "verify"}])
        submit_response = fake_adapter.submit(task_spec=task_spec, workflow_plan=workflow_plan)
        task_id = submit_response.task_id
        
        # Initial - queued
        status1 = fake_adapter.status(task_id)
        assert status1.status == TaskStatus.QUEUED
        
        # After second check - running
        status2 = fake_adapter.status(task_id)
        assert status2.status == TaskStatus.RUNNING
        assert status2.progress_pct == 0.0  # Just started running
        
        # Eventually - completed (need 3 more checks to reach 100%: 25%, 50%, 100%)
        for _ in range(3):
            status = fake_adapter.status(task_id)
        
        assert status.status == TaskStatus.COMPLETED
        assert status.progress_pct == 100.0
        assert len(status.steps_completed) > 0
    
    def test_fake_pause_resume(self, fake_adapter):
        task_spec = TaskSpec(objective="test", task_type="code")
        workflow_plan = WorkflowPlan(steps=[{"action": "implement"}, {"action": "verify"}])
        submit_response = fake_adapter.submit(task_spec=task_spec, workflow_plan=workflow_plan)
        task_id = submit_response.task_id
        
        # Advance to RUNNING (2 status checks: 1st stays QUEUED, 2nd transitions to RUNNING)
        fake_adapter.status(task_id)
        fake_adapter.status(task_id)
        
        # Pause
        pause_response = fake_adapter.pause(task_id, reason="Testing pause")
        assert pause_response.success is True
        assert pause_response.new_status == TaskStatus.PAUSED
        
        status = fake_adapter.status(task_id)
        assert status.status == TaskStatus.PAUSED
        
        # Resume
        resume_response = fake_adapter.resume(task_id, reason="Testing resume")
        assert resume_response.success is True
        assert resume_response.new_status == TaskStatus.RUNNING
    
    def test_fake_cancel(self, fake_adapter):
        task_spec = TaskSpec(objective="test", task_type="code")
        workflow_plan = WorkflowPlan(steps=[{"action": "implement"}, {"action": "verify"}])
        submit_response = fake_adapter.submit(task_spec=task_spec, workflow_plan=workflow_plan)
        task_id = submit_response.task_id
        
        # Advance to RUNNING
        fake_adapter.status(task_id)
        fake_adapter.status(task_id)
        
        cancel_response = fake_adapter.cancel(task_id, reason="User cancelled")
        assert cancel_response.success is True
        assert cancel_response.new_status == TaskStatus.CANCELLED
        
        status = fake_adapter.status(task_id)
        assert status.status == TaskStatus.CANCELLED
    
    def test_fake_approve_reject(self, fake_adapter):
        task_spec = TaskSpec(objective="test", task_type="code")
        submit_response = fake_adapter.submit(task_spec=task_spec)
        task_id = submit_response.task_id
        
        # Manually set to WAITING_APPROVAL for testing
        fake_adapter._fake_state[task_id]["status"] = TaskStatus.WAITING_APPROVAL
        
        # Approve
        approve_response = fake_adapter.approve(task_id, approver="admin", reason="Approved")
        assert approve_response.success is True
        assert approve_response.new_status == TaskStatus.RUNNING
        
        # Reset for reject test
        fake_adapter._fake_state[task_id]["status"] = TaskStatus.WAITING_APPROVAL
        
        # Reject
        reject_response = fake_adapter.reject(task_id, approver="admin", reason="Not ready")
        assert reject_response.success is True
        assert reject_response.new_status == TaskStatus.REJECTED
    
    def test_fake_replan(self, fake_adapter):
        task_spec = TaskSpec(objective="test", task_type="code")
        workflow_plan = WorkflowPlan(steps=[{"action": "implement"}, {"action": "verify"}])
        submit_response = fake_adapter.submit(task_spec=task_spec, workflow_plan=workflow_plan)
        task_id = submit_response.task_id
        
        # Complete the task and set to failed
        for _ in range(6):
            fake_adapter.status(task_id)
        fake_adapter._fake_state[task_id]["status"] = TaskStatus.FAILED
        
        # Replan
        replan_response = fake_adapter.replan(
            task_id,
            replan_spec={"steps": [{"action": "implement"}, {"action": "verify"}]},
            reason="Retry with more steps",
        )
        assert replan_response.success is True
        assert replan_response.new_status == TaskStatus.QUEUED
        
        # After replan, first status check should be QUEUED
        status = fake_adapter.status(task_id)
        assert status.status == TaskStatus.QUEUED
        assert status.progress_pct == 0.0
    
    def test_fake_result(self, fake_adapter):
        task_spec = TaskSpec(objective="test", task_type="code")
        submit_response = fake_adapter.submit(task_spec=task_spec)
        task_id = submit_response.task_id
        
        # Complete the task
        for _ in range(5):
            status = fake_adapter.status(task_id)
            if status.status == TaskStatus.COMPLETED:
                break
        
        result = fake_adapter.result(task_id)
        assert result.task_id == task_id
        assert result.status == TaskStatus.COMPLETED
        assert result.outcome == "success"
        assert result.verification_passed is True
    
    def test_fake_health_check(self, fake_adapter):
        health = fake_adapter.health_check()
        assert health["status"] == "fake"
        assert health["protocol_version"] == "rufl-adapter/v1"
        assert health["fake_mode"] is True
        assert "capabilities" in health
        assert "trust_boundaries" in health


class TestRealAdapterWithMockTransport:
    """Test real adapter with mocked transport."""
    
    def test_submit_with_transport(self):
        mock_transport = Mock(return_value={
            "task_id": "real-task-123",
            "workflow_id": "real-workflow-456",
            "status": "queued",
            "accepted": True,
            "reason": "Accepted",
            "contract_version": "rufl-adapter/v1",
            "submitted_at": "2024-01-01T00:00:00Z",
        })
        
        adapter = RufloAdapter(transport=mock_transport)
        task_spec = TaskSpec(objective="test", task_type="code")
        
        response = adapter.submit(task_spec=task_spec)
        
        assert response.task_id == "real-task-123"
        assert response.workflow_id == "real-workflow-456"
        assert response.accepted is True
        mock_transport.assert_called_once()
    
    def test_submit_transport_failure(self):
        mock_transport = Mock(side_effect=ConnectionError("Network down"))
        
        adapter = RufloAdapter(transport=mock_transport)
        task_spec = TaskSpec(objective="test", task_type="code")
        
        with pytest.raises(RufloUnavailableError):
            adapter.submit(task_spec=task_spec)
    
    def test_status_with_transport(self):
        mock_transport = Mock(return_value={
            "task_id": "task-123",
            "status": "running",
            "progress_pct": 50.0,
            "current_step": "implementation",
            "steps_completed": ["research"],
            "steps_pending": ["test"],
            "steps_failed": [],
            "cost_usd": 5.0,
            "tokens_used": 1000,
            "verification_results": [],
            "contract_version": "rufl-adapter/v1",
        })
        
        adapter = RufloAdapter(transport=mock_transport)
        response = adapter.status("task-123")
        
        assert response.status == TaskStatus.RUNNING
        assert response.progress_pct == 50.0
    
    def test_control_with_transport(self):
        mock_transport = Mock(return_value={
            "task_id": "task-123",
            "action": TaskAction.PAUSE.value,
            "success": True,
            "previous_status": TaskStatus.RUNNING.value,
            "new_status": TaskStatus.PAUSED.value,
            "contract_version": "rufl-adapter/v1",
            "executed_at": "2024-01-01T00:00:00Z",
        })
        
        adapter = RufloAdapter(transport=mock_transport)
        response = adapter.pause("task-123")
        
        assert response.success is True
        assert response.new_status == TaskStatus.PAUSED
    
    def test_result_with_transport(self):
        mock_transport = Mock(return_value={
            "task_id": "task-123",
            "status": "completed",
            "outcome": "success",
            "output_artifacts": [],
            "output_data": {},
            "verification_passed": True,
            "verification_results": [],
            "cost_usd": 10.0,
            "tokens_used": 5000,
            "latency_ms": 30000,
            "started_at": "2024-01-01T00:00:00Z",
            "completed_at": "2024-01-01T00:01:00Z",
            "replan_count": 0,
            "contract_version": "rufl-adapter/v1",
        })
        
        adapter = RufloAdapter(transport=mock_transport)
        result = adapter.result("task-123")
        
        assert result.outcome == "success"
        assert result.verification_passed is True


class TestTrustBoundaries:
    """Test trust boundary enforcement."""
    
    def test_protected_work_requires_rufl_confirmation(self):
        """Verdict never authorizes protected work without Ruflo confirmation."""
        adapter = build_fake_ruflo_adapter()
        
        # Submit protected work
        task_spec = TaskSpec(
            objective="Deploy to production",
            task_type="deploy",
            metadata={"protected": True, "production_impact": True},
        )
        
        response = adapter.submit(task_spec=task_spec)
        
        # Task should be queued, not auto-approved
        assert response.status == TaskStatus.QUEUED
        
        # Must go through Ruflo's approval workflow
        adapter._fake_state[response.task_id]["status"] = TaskStatus.WAITING_APPROVAL
        
        # Cannot complete without approval
        status = adapter.status(response.task_id)
        assert status.status == TaskStatus.WAITING_APPROVAL
        
        # Only Ruflo approval can proceed
        approve_response = adapter.approve(response.task_id, approver="admin")
        assert approve_response.success is True
        assert approve_response.new_status == TaskStatus.RUNNING
    
    def test_capability_manifest_enforcement(self):
        """Capabilities are enforced on both sides."""
        adapter = build_fake_ruflo_adapter()
        
        # Valid manifest - should work
        manifest = CapabilityManifest(required=["task_submission"])
        task_spec = TaskSpec(objective="test", task_type="code")
        
        response = adapter.submit(task_spec=task_spec, capability_manifest=manifest)
        assert response.accepted is True
        
        # Invalid manifest - requires unknown capability
        bad_manifest = CapabilityManifest(required=["unknown_capability"])
        
        with pytest.raises(RufloValidationError, match="unavailable capabilities"):
            adapter.submit(task_spec=task_spec, capability_manifest=bad_manifest)
    
    def test_approval_requires_identity(self):
        """Approval requires approver identity."""
        adapter = build_fake_ruflo_adapter()
        task_spec = TaskSpec(objective="test", task_type="code")
        submit_response = adapter.submit(task_spec=task_spec)
        task_id = submit_response.task_id
        
        # Set to waiting approval
        adapter._fake_state[task_id]["status"] = TaskStatus.WAITING_APPROVAL
        
        # Approve without approver should fail
        with pytest.raises(RufloApprovalError, match="Approver identity required"):
            adapter.approve(task_id, approver="")


class TestProtocolNegotiation:
    """Test protocol version negotiation."""
    
    def test_supported_version(self):
        assert negotiate_protocol_version("rufl-adapter/v1") == "rufl-adapter/v1"
    
    def test_unsupported_version_fallback(self):
        result = negotiate_protocol_version("rufl-adapter/v99")
        assert result == "rufl-adapter/v1"
    
    def test_protocol_version_constant(self):
        assert RUFLO_ADAPTER_PROTOCOL_VERSION == "rufl-adapter/v1"
        assert "rufl-adapter/v1" in SUPPORTED_PROTOCOL_VERSIONS


class TestFactoryFunctions:
    """Test factory functions."""
    
    def test_build_ruflo_adapter(self):
        adapter = build_ruflo_adapter()
        assert isinstance(adapter, RufloAdapter)
        assert adapter.config.fake_mode is False
    
    def test_build_ruflo_adapter_with_config(self):
        config = RufloAdapterConfig(fake_mode=True)
        adapter = build_ruflo_adapter(config=config)
        assert adapter.config.fake_mode is True
    
    def test_build_fake_ruflo_adapter(self):
        adapter = build_fake_ruflo_adapter(fake_latency_ms=100)
        assert isinstance(adapter, RufloAdapter)
        assert adapter.config.fake_mode is True
        assert adapter.config.fake_latency_ms == 100


class TestErrorHierarchy:
    """Test exception hierarchy and categories."""
    
    def test_base_error_category(self):
        err = RufloAdapterError("test")
        assert err.category == "rufl_adapter_error"
    
    def test_unavailable_error_category(self):
        err = RufloUnavailableError("down")
        assert err.category == "rufl_unavailable"
    
    def test_protocol_error_category(self):
        err = RufloProtocolError("bad protocol")
        assert err.category == "rufl_protocol_error"
    
    def test_capacity_error_category(self):
        err = RufloCapacityError("exceeded")
        assert err.category == "rufl_capacity_exceeded"
    
    def test_approval_error_category(self):
        err = RufloApprovalError("denied")
        assert err.category == "rufl_approval_error"
    
    def test_cancellation_error_category(self):
        err = RufloCancellationError("failed")
        assert err.category == "rufl_cancellation_error"
    
    def test_verification_error_category(self):
        err = RufloVerificationError("failed")
        assert err.category == "rufl_verification_error"
    
    def test_timeout_error_category(self):
        err = RufloTimeoutError("timeout")
        assert err.category == "rufl_timeout"
    
    def test_validation_error_category(self):
        err = RufloValidationError("invalid")
        assert err.category == "rufl_validation_error"


class TestCapabilityManifestValidation:
    """Test capability manifest validation against adapter."""
    
    def test_validate_manifest_success(self):
        adapter = build_fake_ruflo_adapter()
        manifest = CapabilityManifest(required=["task_submission", "status_query"])
        
        valid, issues = adapter.validate_capability_manifest(manifest)
        assert valid is True
        assert issues == []
    
    def test_validate_manifest_missing_required(self):
        adapter = build_fake_ruflo_adapter()
        manifest = CapabilityManifest(required=["task_submission", "unknown_capability"])
        
        valid, issues = adapter.validate_capability_manifest(manifest)
        assert valid is False
        assert any("unknown_capability" in issue for issue in issues)
    
    def test_validate_manifest_forbidden_overlap(self):
        # Create adapter with custom capabilities
        config = RufloAdapterConfig(
            required_capabilities=["task_submission"],
            optional_capabilities=[],
        )
        adapter = RufloAdapter(config=config)
        
        # This would be caught at manifest creation time (required + forbidden overlap)
        with pytest.raises(ValueError, match="cannot be both required and forbidden"):
            CapabilityManifest(required=["task_submission"], forbidden=["task_submission"])


class TestIntegrationWithContracts:
    """Test integration with verdict contracts."""
    
    def test_submit_with_task_spec_and_workflow_plan(self):
        adapter = build_fake_ruflo_adapter()
        
        task_spec = TaskSpec(
            objective="Implement user authentication",
            task_type="implement",
            criticality="high",
            budget={"max_usd": 10.0},
            required_capabilities=["tool-calling"],
            destructive_operation=False,
            production_impact=False,
        )
        
        workflow_plan = WorkflowPlan(
            steps=[
                {"action": "implement", "objective": "Implement auth module"},
                {"action": "verify", "objective": "Run tests"},
            ],
            verification=VerificationPlan(checks=["tests"], on_failure="replan_or_deny"),
        )
        
        response = adapter.submit(
            task_spec=task_spec,
            workflow_plan=workflow_plan,
            budget_usd=10.0,
        )
        
        assert response.accepted is True
        assert response.task_id is not None
        assert response.workflow_id is not None
        
        # Verify serialization
        task_data = adapter._fake_state[response.task_id]
        assert "task_spec" in task_data["request"]
        assert "workflow_plan" in task_data["request"]
        assert task_data["request"]["task_spec"]["objective"] == "Implement user authentication"
    
    def test_capability_manifest_from_task_spec(self):
        adapter = build_fake_ruflo_adapter()
        
        task_spec = TaskSpec(
            objective="test",
            task_type="code",
            required_capabilities=["task_submission", "status_query"],
        )
        
        manifest = CapabilityManifest(required=task_spec.required_capabilities)
        
        response = adapter.submit(task_spec=task_spec, capability_manifest=manifest)
        assert response.accepted is True


class TestConfigDefaults:
    """Test configuration defaults."""
    
    def test_default_config(self):
        config = RufloAdapterConfig()
        assert config.protocol_version == "rufl-adapter/v1"
        assert config.submit_timeout_ms == 5000
        assert config.fake_mode is False
        assert "task_submission" in config.required_capabilities
        assert config.trust_protected_work is True
        assert config.require_verification_for_protected is True
    
    def test_custom_config(self):
        config = RufloAdapterConfig(
            fake_mode=True,
            fake_latency_ms=50,
            trust_protected_work=False,
        )
        assert config.fake_mode is True
        assert config.fake_latency_ms == 50
        assert config.trust_protected_work is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])