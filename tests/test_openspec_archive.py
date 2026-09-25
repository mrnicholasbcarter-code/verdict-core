"""Test OpenSpec archive preconditions."""

from __future__ import annotations


def can_archive_openspec_change(
    change_id: str, has_merged_main_verification: bool
) -> tuple[bool, str]:
    """Check if an OpenSpec change can be archived.

    Archive is allowed ONLY when merged-main verification evidence is present.

    Args:
        change_id: OpenSpec change identifier
        has_merged_main_verification: Whether merged-main verification passed

    Returns:
        Tuple of (can_archive, reason)
    """
    if not has_merged_main_verification:
        return (False, f"Cannot archive {change_id}: missing merged-main verification evidence")

    return (True, f"Change {change_id} ready for archive")


def test_archive_precondition_requires_merged_main():
    """Archive is blocked without merged-main verification."""
    can_archive, reason = can_archive_openspec_change(
        "bod-205-test", has_merged_main_verification=False
    )

    assert can_archive is False
    assert "merged-main verification" in reason.lower()


def test_archive_precondition_allows_with_merged_main():
    """Archive proceeds when merged-main verification is present."""
    can_archive, reason = can_archive_openspec_change(
        "bod-205-test", has_merged_main_verification=True
    )

    assert can_archive is True
    assert "ready for archive" in reason.lower()
