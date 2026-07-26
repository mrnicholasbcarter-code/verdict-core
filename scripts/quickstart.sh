#!/bin/bash
# Verdict Core Quickstart - Clean Environment Demo
# Run from repository root: ./scripts/quickstart.sh

set -e

echo "🚀 Verdict Core Quickstart - Clean Environment Demo"
echo "=================================================="
echo ""

# Create clean environment
QUICKSTART_DIR="/tmp/verdict-quickstart-$(date +%s)"
echo "📁 Creating clean environment: $QUICKSTART_DIR"
rm -rf "$QUICKSTART_DIR"
mkdir -p "$QUICKSTART_DIR"
cd "$QUICKSTART_DIR"

# Create virtual environment
echo "🐍 Creating virtual environment..."
python3 -m venv venv
source venv/bin/activate

# Install verdict-core from local source (editable)
echo "📦 Installing verdict-core from local source..."
pip install -e /home/nick/dev/verdict-core --quiet

# Run flagship demo
echo "🎭 Running flagship demo..."
echo ""
python /home/nick/dev/verdict-core/scripts/flagship_demo.py

echo ""
echo "✅ Quickstart complete!"
echo "   Environment: $QUICKSTART_DIR"
echo "   To run again: source $QUICKSTART_DIR/venv/bin/activate && python /home/nick/dev/verdict-core/scripts/flagship_demo.py"
echo ""

# Cleanup option
read -p "🗑️  Clean up environment? (y/N) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    deactivate
    rm -rf "$QUICKSTART_DIR"
    echo "🗑️  Cleaned up $QUICKSTART_DIR"
else
    echo "📁 Environment preserved at $QUICKSTART_DIR"
fi
