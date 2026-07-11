# IDE Integration (Cursor / Zed / Cline / Roo Code)

To integrate `llm-gate` directly into your IDE (Cursor, Zed, Cline, or Roo Code), you want the Fast-API microservice mode.

### 1. Launch the Server
Run the built-in FastAPI microservice:
```bash
llm-gate serve --port 8000
```

### 2. Configure Your IDE

Because `llm-gate` focuses purely on routing decisions (to avoid adding latency overhead to streaming tokens), the recommended IDE pattern is to pair it with an execution proxy like **LiteLLM**.

In your IDE AI configuration (e.g., `config.json` for Continue.dev):

```json
{
  "models": [
    {
      "title": "llm-gate Auto-Router",
      "provider": "openai",
      "model": "dynamic",
      "apiBase": "http://localhost:8000/v1"
    }
  ]
}
```

*(Note: Advanced proxying of the actual `chat/completions` byte-stream is actively being built for `v0.3.0` which will allow `llm-gate` to act as a 100% native drop-in replacement for OpenAI endpoints in your IDE.)*
