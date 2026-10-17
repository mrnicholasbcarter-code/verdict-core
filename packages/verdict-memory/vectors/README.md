# Golden vectors (language-neutral)

These JSON files are the Rust/Python compatibility fixtures for
`lifecycle-event/v1`. A native consumer must produce the same `digest` for
the same logical event (observed_at excluded from the digest payload).

Serialization convention (existing Verdict shared-memory style):
`json.dumps(..., sort_keys=True, separators=(",", ":"), ensure_ascii=False)`
then UTF-8 bytes into SHA-256.
