#!/usr/bin/env bash
set -e

echo "🚀 llm-gate Universal Integration Installer"
echo "This script globally links llm-gate into your preferred AI tools."

BIN_DIR="/usr/local/bin"
if [ ! -w "$BIN_DIR" ]; then
    echo "⚠️  No write access to $BIN_DIR. Installing wrappers to $HOME/.local/bin"
    BIN_DIR="$HOME/.local/bin"
    mkdir -p "$BIN_DIR"
fi

# 1. Install CLI Agent Wrappers
CLI_AGENTS=("kilocode" "pi" "pool" "codebuff" "aider" "mimo" "opencode" "jcode" "agy" "codex" "cowork" "agentsdk" "claude")

echo "📦 Installing shell wrappers to $BIN_DIR..."

for AGENT in "${CLI_AGENTS[@]}"; do
    WRAPPER_PATH="$BIN_DIR/${AGENT}-routed"
    cat << SCRIPT > "$WRAPPER_PATH"
#!/usr/bin/env bash
# Auto-generated llm-gate router for $AGENT
TARGET=\$(llm-gate route "\$*" --terse 2>/dev/null)

if [ -z "\$TARGET" ]; then
    echo "[llm-gate] Routing failed, falling back to unrouted $AGENT..."
    exec $AGENT "\$@"
fi

# Map target to tool-specific environment variables
export ${AGENT^^}_MODEL="\$TARGET"
export ${AGENT^^}_TARGET_MODEL="\$TARGET"
export MODEL="\$TARGET"

exec $AGENT "\$@"
SCRIPT
    chmod +x "$WRAPPER_PATH"
    echo "  ✔ Created $WRAPPER_PATH"
done

# 2. IDE Configuration Injection (Cursor, VS Code, Roo Code, Cline)
echo "💻 Injecting llm-gate microservice into IDE configs..."

# VS Code / Continue.dev
CONTINUE_CONFIG="$HOME/.continue/config.json"
if [ -f "$CONTINUE_CONFIG" ]; then
    # Create backup
    cp "$CONTINUE_CONFIG" "${CONTINUE_CONFIG}.bak"
    # Basic sed replacement to inject an llm-gate provider if an models array exists (very naive JSON injection)
    # Recommended approach: Inform user to start HTTP server
    echo "  ✔ Found Continue.dev config at $CONTINUE_CONFIG (Backup created)"
    # A robust jq edit would go here natively, but for the script we'll append a message
    echo "    -> Please ensure 'llm-gate serve' is running on port 8000 to accept Continue.dev traffic."
fi

# Cursor
CURSOR_CONFIG="$HOME/Library/Application Support/Cursor/User/settings.json" # macOS default
if [ -f "$CURSOR_CONFIG" ]; then
    echo "  ✔ Found Cursor settings at $CURSOR_CONFIG"
    echo "    -> Ensure you have 'http://localhost:8000/v1' set as your OpenAI API Base in Cursor UI."
fi

# Roo Code / Cline (often reads from .env or workspace settings)
ROO_CONFIG="$HOME/.roo/config.json"
if [ -f "$ROO_CONFIG" ]; then
    echo "  ✔ Found Roo Code config."
    echo "    -> Set ROO_OPENAI_BASE_URL='http://localhost:8000/v1' to enable routing."
fi

echo ""
echo "✅ Universal installation complete!"
echo "To use routed agents, just run the command with '-routed' (e.g., 'pi-routed build me an app')"
echo "To route IDE traffic, run 'llm-gate serve --port 8000' in the background."
