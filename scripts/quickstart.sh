#!/bin/bash
# Verdict Core quickstart: the two offline steps of the README Quick start, run in a
# throwaway virtual environment and a throwaway state directory.
#
#   ./scripts/quickstart.sh          (run it from any directory inside or outside the clone)
#
# It needs python3 (3.10 or newer) and network access for `pip install`. It needs no
# gateway, no API key and no model call. It does not touch your real ~/.verdict: the
# state directory is inside the temporary directory. It is not the live run: that is
# README Quick start, step 3.

set -euo pipefail

# The repository root comes from this script's own location, never from a fixed path.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "Verdict Core quickstart (offline)"
echo "================================="
echo ""

# Create a clean environment
QUICKSTART_DIR="$(mktemp -d "${TMPDIR:-/tmp}/verdict-quickstart.XXXXXX")"
echo "Creating clean environment: $QUICKSTART_DIR"
export VERDICT_HOME="$QUICKSTART_DIR/verdict-home"
mkdir -p "$VERDICT_HOME"
cd "$QUICKSTART_DIR"

echo "Creating virtual environment..."
python3 -m venv venv
# shellcheck disable=SC1091
source venv/bin/activate

# Install verdict-core from this clone (editable)
echo "Installing verdict-core from $REPO_ROOT..."
pip install -e "$REPO_ROOT" --quiet

# Step 1 of the README Quick start: one routing decision from a fixture
echo ""
echo "Step 1: verdict quickstart --non-interactive --dry-run"
echo ""
verdict quickstart --non-interactive --dry-run

# Step 2 of the README Quick start: the whole loop offline, scripted workers
echo ""
echo "Step 2: verdict demo"
echo ""
verdict demo

echo ""
echo "Quickstart complete. Both steps ran offline; nothing here was a live run."
echo "   Environment: $QUICKSTART_DIR"
echo "   To run again: source $QUICKSTART_DIR/venv/bin/activate && VERDICT_HOME=$VERDICT_HOME verdict demo"
echo ""

# Cleanup: ask only when a person is at the keyboard; otherwise keep it and say where it is.
if [ -t 0 ]; then
    read -p "Clean up environment? (y/N) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        deactivate
        rm -rf "$QUICKSTART_DIR"
        echo "Cleaned up $QUICKSTART_DIR"
        exit 0
    fi
fi
echo "Environment preserved at $QUICKSTART_DIR"
