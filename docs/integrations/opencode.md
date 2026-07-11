# OpenCode Integration

[OpenCode] is a powerful open-source coding agent that relies heavily on contextual awareness. To prevent OpenCode from burning your frontier model quota on simple file-reads or git-diff summaries, you can wrap it with `llm-gate`.

### Configuration

OpenCode supports standard environment variable configuration for its target model.

Create an alias in your `.bashrc` or `.zshrc`:

```bash
opencode_routed() {
    local task="$*"
    # llm-gate evaluates the first prompt 
    TARGET=$(llm-gate route "$task" | grep -o '• Model: .*' | awk '{print $3}' | sed 's/\[.*\]//g')
    
    # Inject into OpenCode's environment
    OPENCODE_MODEL="$TARGET" opencode "$@"
}
```

This ensures OpenCode dynamically degrades its model for repetitive file operations, but snaps back to `opus-4.8` when the task contains "architecture" or "security" keywords.
