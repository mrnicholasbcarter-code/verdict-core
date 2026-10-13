"""Main entry point for the verdict package."""

from verdict.cli import main
from verdict.gate import Gate

DRILL_LEGACY_SELECTOR = Gate  # Drill: a new production reference to the legacy selector.

if __name__ == "__main__":
    main()
