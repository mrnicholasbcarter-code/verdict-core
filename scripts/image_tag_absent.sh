#!/usr/bin/env bash
# Exit 0 only when the registry confirms that <image-ref:tag> does not exist.
# Exit 1 when it exists, or when absence cannot be confirmed (fail closed).
# Usage: scripts/image_tag_absent.sh <image-ref:tag>
set -euo pipefail
ref="${1:?usage: image_tag_absent.sh <image-ref:tag>}"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
# Capture to a file, not a shell variable: command substitution would drop trailing
# newlines and NUL bytes, so a look-alike answer could compare equal.
if docker manifest inspect "$ref" >"$tmp/out" 2>&1; then
  echo "image $ref already exists" >&2
  exit 1
fi
# The whole answer must equal, byte for byte, one of the forms the Docker CLI prints for a
# missing manifest, with or without its single terminating newline. Anything else (other
# lines, blank lines, other bytes) means absence is not confirmed.
for form in "manifest unknown" "manifest unknown: manifest unknown" "no such manifest: $ref"; do
  printf '%s' "$form" >"$tmp/bare"
  printf '%s\n' "$form" >"$tmp/terminated"
  if cmp -s "$tmp/out" "$tmp/bare" || cmp -s "$tmp/out" "$tmp/terminated"; then
    exit 0
  fi
done
echo "could not confirm that $ref is absent: $(head -c 500 "$tmp/out" | tr -d '\000')" >&2
exit 1
