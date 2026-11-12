# Tasks

Owned files: `verdict/discovery.py` (`fetch_models`), `tests/test_bod416_route_key_file_fallback.py`
and this change.

- [x] 1.1 Failing tests first: key-file-only discovery sends no key, and the route falls back
  to the primary model while the exported key routes to the catalog model.
- [x] 1.2 In `fetch_models`, when a declared gateway key env name is unset, fall back to
  `credentials_store.resolve_gateway_key`.
- [x] 1.3 Guard tests: an exported key still wins over the key file, and other providers never
  receive the gateway key.
