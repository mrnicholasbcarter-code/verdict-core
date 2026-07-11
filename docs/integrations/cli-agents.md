# Terminal & CLI Agents 
*(Kilo Code, pi / Oh-My-Pi, Codebuff, Poolside, Aider)*

Terminal-based coding agents execute hundreds of prompts reading configurations, listing directories, and formatting files. Using `opus-4.8` for this is a massive waste of API quota.

Wrap these tools in your `.bashrc` or `.zshrc` using `llm-gate route --terse`.

**Kilo Code:**
```bash
kilocode_routed() {
    export KILO_TARGET=$(llm-gate route "$*" --terse)
    kilo "$@"
}
```

**pi & Oh-My-Pi (omp.sh):**
```bash
pi_routed() {
    export PI_MODEL=$(llm-gate route "$*" --terse)
    pi "$@"
}
```

**Poolside (pool):**
```bash
pool_routed() {
    pool --model $(llm-gate route "$*" --terse) "$@"
}
```

**Codebuff:**
```bash
codebuff_routed() {
    codebuff --model $(llm-gate route "$*" --terse) "$@"
}
```
