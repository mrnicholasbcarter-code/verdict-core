#!/usr/bin/env bash
# Exit 0 only when the registry confirms that <image-ref:tag> does not exist.
# Exit 1 when it exists, or when absence cannot be confirmed (fail closed).
# Usage: scripts/image_tag_absent.sh <image-ref:tag>
set -euo pipefail
ref="${1:?usage: image_tag_absent.sh <image-ref:tag>}"
# A sentinel after the output keeps every trailing newline: plain $(...) would strip them.
raw=$(
  set +e
  docker manifest inspect "$ref" 2>&1
  printf '\n#rc=%d' "$?"
)
rc=${raw##*#rc=}
out=${raw%$'\n'#rc=*}
if [ "$rc" = "0" ]; then
  echo "image $ref already exists" >&2
  exit 1
fi
# The whole answer must be exactly one of the forms the Docker CLI prints for a missing
# manifest, with at most its single terminating newline. Nothing is stripped: any other
# line, blank or not, before or after, means absence is not confirmed.
nl=$'\n'
for form in "manifest unknown" "manifest unknown: manifest unknown" "no such manifest: $ref"; do
  if [ "$out" = "$form" ] || [ "$out" = "$form$nl" ]; then
    exit 0
  fi
done
echo "could not confirm that $ref is absent: $out" >&2
exit 1
