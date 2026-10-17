# lifecycle-event/v1 same-byte golden vectors

Parity fixtures for the language-neutral `lifecycle-event/v1` seam. The Python
reference (`verdict_memory.contracts`) and the native Rust adapter must
produce **these exact bytes**. Do not trust a matching digest that was computed
some other way.

Regenerate with `python scripts/gen_golden_vectors.py`. Check with `--check`.
The wheel ships this directory as `verdict_memory/vectors/golden/lifecycle-event-v1/`
and the generator as `verdict_memory/scripts/gen_golden_vectors.py`, so
`python <site-packages>/verdict_memory/scripts/gen_golden_vectors.py --check`
also works from an installed wheel.
Do not edit a fixture by hand. Add a new case instead, because a changed byte
is a contract change. Changing a byte also needs a deliberate update to the
pinned digests in `tests/test_golden_vectors_same_byte.py`.

## Layout

```
manifest.json                  expected hashes + sha256 of every case file
SHA256SUMS                     `sha256sum -c SHA256SUMS` format, every fixture file
valid/<case>/input.json        wire input a harness may send (optional fields omitted, extras allowed)
valid/<case>/content.txt       raw UTF-8 content;          sha256 == content_hash
valid/<case>/canonical.json    canonical full event bytes (includes observed_at, deadline_at, idempotency_key)
valid/<case>/digest_payload.json       canonical minus content, observed_at, deadline_at, idempotency_key; sha256 == digest
valid/<case>/fingerprint_payload.json  digest payload minus created_at; sha256 == content_fingerprint (only when source_event_id is set)
valid/<case>/idempotency_payload.json  {schema_version, protocol_version, source_harness, project, workspace, tenant, session_id, source_event_id}; sha256 == idempotency_key (only when source_event_id is set)
invalid/<case>.json            input that must be rejected; manifest gives a language-neutral error code
```

Without a `source_event_id`, `idempotency_key == content_fingerprint == digest`.

## Canonical bytes

`json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)`
encoded as UTF-8, with **no trailing newline**. In practice:

- Object keys sort by Unicode code point at every depth (the same as UTF-8 byte order). Arrays keep their order.
- Non-ASCII text stays as raw UTF-8. Only `"`, `\` and C0 controls are escaped
  (`\b \f \n \r \t` short forms, everything else as lowercase `\u00XX`). DEL,
  NBSP, U+2028/U+2029 and characters outside the BMP stay raw. Nothing is
  NFC-normalized.
- A float keeps its shortest round-trip spelling with `.0` (`1700000000.0`, `-0.0`). An integer stays an integer.
  The pinned values avoid exponent notation, where Python (`1e-07`, `1e+16`)
  and Rust/ryu (`1e-7`, `1e16`) disagree. Keep fixture floats in `[1e-4, 1e16)`.
- NaN and Infinity are rejected.

## Rust consumer notes (measured, scratch crate, serde_json 1.0.151, sha2 0.10.9)

- `serde_json` **with `float_roundtrip`** and the default sorted `Map`: all 129 checks pass. That covers
  bytes of every file, canonical round-trip, rebuilt digest/fingerprint/idempotency payloads, and NaN rejection.
- Without `float_roundtrip`: `numeric_pinning` fails (5 checks). Parsing `999999999999999.9` loses a ULP.
- With `preserve_order` (the prime-agent workspace setting): 45 checks fail
  because rebuilt payloads keep insertion order. A native canonicalizer must sort
  keys explicitly and must not rely on the `Map` type.

Loading in a Rust test: `include_bytes!("…/valid/<case>/canonical.json")`, then
compare your serializer's output byte-for-byte and `sha2::Sha256::digest` against
`manifest.json` → `valid.<case>.expect`.
