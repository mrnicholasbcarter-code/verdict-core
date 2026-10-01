#!/usr/bin/env bash
# Exit 0 only when the registry confirms that <image-ref:tag> does not exist.
# Exit 1 when it exists, or when absence cannot be confirmed (fail closed).
# Usage: scripts/image_tag_absent.sh <image-ref:tag>
set -euo pipefail
ref="${1:?usage: image_tag_absent.sh <image-ref:tag>}"
if out=$(docker manifest inspect "$ref" 2>&1); then
  echo "image $ref already exists" >&2
  exit 1
fi
# The whole answer (command substitution drops only trailing newlines) must be exactly
# one of the forms the Docker CLI prints for a missing manifest. Nothing is stripped:
# extra lines, blank or not, mean the answer is not a confirmed absence.
case "$out" in
  "manifest unknown" | "manifest unknown: manifest unknown" | "no such manifest: $ref")
    exit 0
    ;;
esac
echo "could not confirm that $ref is absent: $out" >&2
exit 1
