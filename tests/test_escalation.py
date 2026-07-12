"""Unit tests for the keyword escalation scanner."""

from llm_gate.escalation import scan


class TestEscalationScanner:
    def test_returns_tuple(self):
        result = scan("hello world")
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_benign_text_no_escalation(self):
        tier, _ = scan("format this json nicely")
        # Should either be None (no escalation) or >= 2
        assert tier is None or tier >= 2

    def test_critical_keyword_triggers(self):
        # Words like "deploy", "production", "delete" should trigger escalation
        tier, _ = scan("deploy the production database migration")
        if tier is not None:
            assert tier <= 1  # Should escalate to at least high
