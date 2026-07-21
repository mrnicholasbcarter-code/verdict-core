#!/usr/bin/env bash
set -e

echo "🛑 Verdict Uninstaller"

PURGE_CONFIG=false
PURGE_LOGS=false
PURGE_ALL=false

while [[ "$#" -gt 0 ]]; do
    case $1 in
        --purge-all) PURGE_ALL=true; PURGE_CONFIG=true; PURGE_LOGS=true ;;
        --purge-config) PURGE_CONFIG=true ;;
        --purge-logs) PURGE_LOGS=true ;;
        --help)
            echo "Usage: curl -sSL https://.../uninstall.sh | bash -s -- [options]"
            echo "Options:"
            echo "  --purge-all     Remove everything (package, configs, logs)"
            echo "  --purge-config  Remove the config directory (config.yaml)"
            echo "  --purge-logs    Remove all verdict-decisions.jsonl files"
            exit 0
            ;;
        *) echo "Unknown parameter passed: $1"; exit 1 ;;
    esac
    shift
done

echo "📦 Removing Python package..."
if command -v pipx &> /dev/null && pipx list | grep -q verdict-core; then
    pipx uninstall verdict-core
elif command -v pip &> /dev/null; then
    pip uninstall -y verdict-core || true
fi

CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/verdict"

if [ "$PURGE_CONFIG" = true ]; then
    echo "🗑️ Removing configurations at $CONFIG_DIR..."
    rm -rf "$CONFIG_DIR"
else
    echo "ℹ️ Kept configuration files intact at $CONFIG_DIR (use --purge-config to remove)"
fi

if [ "$PURGE_LOGS" = true ]; then
    echo "🗑️ Removing routing logs..."
    # Naive search for logs in common locations
    rm -f ./verdict-decisions.jsonl
    rm -f "$HOME/verdict-decisions.jsonl"
else
    echo "ℹ️ Kept routing logs intact (use --purge-logs to remove)"
fi

echo "✅ Verdict successfully uninstalled."
