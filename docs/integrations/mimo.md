# Mimo Integration

When integrating `llm-gate` with Mimo, you want to enforce strict budgetary boundaries for automated development workflows. 

### The Wrapper Approach

Run Mimo commands seamlessly through our shell interceptor:

```bash
# Add to ~/.bash_profile
mimo_routed() {
    local prompt="$1"
    MODEL_OVERRIDE=$(llm-gate route "$prompt" --criticality medium | grep -o '• Model: .*' | awk '{print $3}' | sed 's/\[.*\]//g')
    
    MIMO_TARGET_MODEL="$MODEL_OVERRIDE" mimo "$@"
}
```
