"""Main entry point for the verdict package."""

from verdict.cli import main

if __name__ == "__main__":
    main()


def drill_unused() -> None:
    """Drill: an unused function that is baselined instead of wired."""
