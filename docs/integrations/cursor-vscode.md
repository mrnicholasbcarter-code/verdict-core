# IDE Integration (Cursor / Zed / Cline / Roo Code)

To integrate `llm-gate` directly into your IDE (Cursor, Zed, Cline, or Roo Code), you want the Fast-API microservice mode.

### 1. Launch the Server
Run the built-in FastAPI microservice:
```bash
llm-gate serve --port 8000
```

### 2. Configure Your IDE

`llm-gate` now has an alpha `POST /v1/chat/completions` proxy that preserves request fields and streamed bytes. For production IDE use, continue pairing it with LiteLLM or another execution proxy until local auth, live availability, retry/fallback, and compatibility gates pass.

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

*(Note: the native proxy path is an alpha development slice, not yet a 100% production drop-in replacement. See the [release acceptance matrix](../specs/RELEASE_ACCEPTANCE.md) before using it with real credentials.)*
