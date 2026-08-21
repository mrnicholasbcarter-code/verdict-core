from pathlib import Path


def test_user_journey_commands_are_present_and_truthful():
    readme = Path("README.md").read_text()
    journey = Path("docs/USER_JOURNEY.md").read_text()
    assert "verdict --help" in journey
    assert "verdict detect" in journey
    assert "autodev-golden-path" in journey
    assert "verdict replay" in journey
    assert "3500+ models" not in readme
    assert "OMNIROUTE (Intelligent Model Router)" not in readme
