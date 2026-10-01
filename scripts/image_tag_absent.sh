#!/usr/bin/env bash
# Exit 0 only when the registry confirms that IMAGE_REF:TAG does not exist.
# Exit 1 when it exists, or when absence cannot be confirmed (fail closed).
# Usage: scripts/image_tag_absent.sh <image-ref:tag>
set -euo pipefail
ref="${1:?usage: image_tag_absent.sh <image-ref:tag>}"
if out=$(docker manifest inspect "$ref" 2>&1); then
  echo "image $ref already exists" >&2
  exit 1
fi
# Absence must be the ONLY thing the registry said: one non-empty line, in one of
# the two exact forms the Docker CLI prints for a missing manifest. Anything else
# (auth, rate limit, network, mixed output) is not a confirmed absence.
lines=$(printf '%s\n' "$out" | sed '/^[[:space:]]*$/d')
if [ "$(printf '%s\n' "$lines" | wc -l)" -eq 1 ] \
  && printf '%s\n' "$lines" | grep -Eq "^(no such manifest: ${ref//./\\.}|manifest unknown(: manifest unknown)?)$"; then
  exit 0
fi
echo "could not confirm that $ref is absent: $out" >&2
exit 1
