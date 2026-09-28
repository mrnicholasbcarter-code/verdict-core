# Architecture: receipt digest

The uplift receipt digest is computed as `BLAKE2s-128` over the canonical event log
and stored in the field `uplift_digest_b2s`. The digest is prefixed with the literal
marker `ud1:` so older SHA-256 receipts remain distinguishable.

The maximum packed file size is `8192 bytes`; a larger file is truncated and flagged
with `truncated_by_cap` in the same receipt.
