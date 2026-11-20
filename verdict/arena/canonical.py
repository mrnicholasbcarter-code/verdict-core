"""Arena model-name canonicaliser.

Exact (org, key) join — no fuzzy matching. Every parse outcome is either
a Parsed (match candidate) or a Drop (named, never silent). The function
is deterministic: same input → same output.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# Closed vocabularies (case-insensitive comparison)
EFFORT_VOCAB: frozenset[str] = frozenset({"minimal", "low", "medium", "high", "xhigh", "max"})
MODE_VOCAB: frozenset[str] = frozenset({"thinking", "reasoning", "instant"})
_HARNESS_VOCAB: frozenset[str] = frozenset({"codex harness"})

# Patterns matched case-insensitively after lowercasing the token
_DATE_PATTERN = re.compile(r"^\d{8}$|^\d{4}$|^\d{2}-\d{2}(-\d{2})?$")
_RECORDED_ONLY_PATTERN = re.compile(r"^\d+k$|^nvfp4$")
_SLUG_REPLACE = re.compile(r"[ ._:]+")
_SLUG_COLLAPSE = re.compile(r"-+")
_SLUG_VALID = re.compile(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$|^[a-z0-9]$")

DropReason = str  # named constant strings below

DROP_HARNESS_VARIANT = "harness_variant"
DROP_UNKNOWN_QUALIFIER = "unknown_qualifier"
DROP_ORG_UNMAPPED = "org_unmapped"
DROP_UNPARSEABLE_NAME = "unparseable_name"
DROP_NO_STORE_RECORD = "no_store_record"
DROP_VARIANT_COLLISION = "variant_collision"
DROP_CROSSWALK_DROP = "crosswalk_drop"
DROP_CROSSWALK_TARGET_MISSING = "crosswalk_target_missing"


@dataclass(frozen=True)
class Parsed:
    """Canonicalised Arena row: ready for store-index lookup."""

    org: str
    key: str
    effort: str  # effort slot, e.g. "high", "max", "default"
    qualifiers: tuple[str, ...] = ()


@dataclass(frozen=True)
class Drop:
    """An Arena row that cannot be matched, with a named reason."""

    model_name: str
    organization: str
    reason: DropReason
    detail: str = ""


def slug(text: str) -> str:
    """Canonical slug: lowercase, replace runs of [ ._:] with -, collapse and strip."""
    lowered = text.lower()
    replaced = _SLUG_REPLACE.sub("-", lowered)
    collapsed = _SLUG_COLLAPSE.sub("-", replaced)
    return collapsed.strip("-")


def _is_valid_slug(s: str) -> bool:
    return bool(_SLUG_VALID.match(s)) if s else False


def parse_name(
    model_name: str, organization: str, org_providers: dict[str, list[str]]
) -> Parsed | Drop:
    """Canonicalise an Arena model_name + organization into a Parsed or Drop.

    Rules (applied in order):
    1. NFKC normalise + trim + collapse internal whitespace.
    2. If name ends in exactly one (...) group, extract and classify tokens.
    3. slug(base) → key (with optional mode/date suffixes).
    4. org from org_providers table.
    """
    # Step 1: normalise
    normalised = unicodedata.normalize("NFKC", model_name).strip()
    normalised = re.sub(r"\s+", " ", normalised)

    # Step 2: extract parenthetical suffix (exactly one `(...)` at end)
    paren_match = re.match(r"^(.*?)\s*\(([^()]+)\)\s*$", normalised)
    base: str
    paren_tokens: list[str] = []
    if paren_match:
        base = paren_match.group(1).strip()
        paren_tokens = [t.strip() for t in paren_match.group(2).split(",")]
    else:
        # Check for nested parens → unparseable
        if normalised.count("(") > 0 or normalised.count(")") > 0:
            return Drop(
                model_name=model_name,
                organization=organization,
                reason=DROP_UNPARSEABLE_NAME,
                detail="nested or unbalanced parens",
            )
        base = normalised

    # Classify paren tokens
    effort = "default"
    modes: list[str] = []
    dates: list[str] = []
    qualifiers: list[str] = []
    for token in paren_tokens:
        tl = token.lower()
        if tl in EFFORT_VOCAB:
            effort = tl
        elif tl in MODE_VOCAB:
            modes.append(tl)
        elif _DATE_PATTERN.match(tl):
            dates.append(tl)
        elif tl in _HARNESS_VOCAB:
            return Drop(
                model_name=model_name,
                organization=organization,
                reason=DROP_HARNESS_VARIANT,
                detail=f"harness token: {token!r}",
            )
        elif _RECORDED_ONLY_PATTERN.match(tl):
            qualifiers.append(token)
        else:
            return Drop(
                model_name=model_name,
                organization=organization,
                reason=DROP_UNKNOWN_QUALIFIER,
                detail=f"unknown token: {token!r}",
            )

    # Step 3: build key
    base_slug = slug(base)
    if not _is_valid_slug(base_slug):
        return Drop(
            model_name=model_name,
            organization=organization,
            reason=DROP_UNPARSEABLE_NAME,
            detail=f"slug {base_slug!r} does not match [a-z0-9-]+",
        )
    key_parts = [base_slug, *modes, *dates]
    key = "-".join(key_parts)

    # Step 4: map org
    if organization not in org_providers:
        return Drop(
            model_name=model_name,
            organization=organization,
            reason=DROP_ORG_UNMAPPED,
            detail=f"organization {organization!r} not in org_providers",
        )

    return Parsed(org=organization, key=key, effort=effort, qualifiers=tuple(qualifiers))
