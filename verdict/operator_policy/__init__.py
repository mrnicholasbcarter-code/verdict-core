"""Versioned approved operator policy authority for native execution boundaries.

One versioned, immutable-digest rules source ships inside this package
(``verdict/operator_policy/v1/``).  Each rule is tied to a verified operator
decision, a Linear issue, a scope, a matcher, a deny reason and an approved
override policy.

Memory pages and LLM summaries are EVIDENCE ONLY: retrieval never edits,
approves, or promotes rules, and never waives a check.

Unknown / missing / corrupt / stale / unapproved policy fails closed for
protected actions with a named error; diagnostics remain available.

Design invariants
-----------------
* Fail closed: a missing, stale, corrupt or unapproved policy source denies.
* Advisory never weakens hard policy: only compiled rules decide.  Text that
  arrived through memory retrieval is never a rule.
* Canonicalize argv/env/path/symlinks/CWD before matching, so wrappers,
  aliases, symlinks and working-dir tricks cannot bypass protected actions.
* Zero secrets in audit records: values are fingerprinted or redacted.
* Single authority: no competing policy service, no blanket ``skip safety``
  flag.  Non-waivable controls (vgate mutation, no secret in code,
  destructive interference with running work) carry no override policy.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from verdict.security import fingerprint_text, redact_text

POLICY_SCHEMA_VERSION = "verdict.operator-policy/v1"
SUPPORTED_POLICY_VERSION = 1
ENV_POLICY_DIR = "VERDICT_OPERATOR_POLICY_DIR"
DEFAULT_POLICY_DIR = Path(__file__).resolve().parent / "v1"

MANIFEST_NAME = "manifest.json"
RULES_NAME = "rules.json"

_LEGACY_ENV_PREFIX = "llmgate_"
_NEW_ENV_PREFIX = "verdict_"


class PolicyUnavailableError(RuntimeError):
    """The approved policy source cannot be loaded or verified.

    Boundaries must fail closed with this named error; they must not treat
    the absence as "no policy, therefore allow".
    """


class OverrideRejectedError(RuntimeError):
    """An approved exception token failed validation."""


class Decision(str, Enum):
    """Hard-policy outcomes; ``unknown`` is never coerced to ``allow``."""

    ALLOW = "allow"
    DENY = "deny"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class OverridePolicy:
    """Approved exception terms for one rule.

    Tokens are bound to operator identity, exact action/scope, expiry,
    single-use nonce and policy digest; replayed, expired, wrong-scope or
    self-issued tokens fail.
    """

    requires_operator: str
    bound_action: str
    bound_scope: str
    expiry_seconds: int
    nonce_required: bool = True
    approval_ref: str = ""


@dataclass(frozen=True)
class Rule:
    """One compiled approved policy rule."""

    rule_id: str
    title: str
    effect: str  # "allow" or "deny"
    scope: str
    matcher: dict[str, Any]
    deny_reason: str
    override: OverridePolicy | None
    waivable: bool
    enabled: bool
    precedence: int  # lower = evaluated first; first match wins
    decision_refs: tuple[str, ...] = ()
    linear_issue: str = ""


@dataclass(frozen=True)
class ActionContext:
    """Description of one protected action, canonicalized before matching."""

    action: str
    argv: tuple[str, ...] = field(default_factory=tuple)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    worktree: str | None = None
    actor: str | None = None
    linear_issue: str | None = None
    acceptance_criteria: tuple[str, ...] = field(default_factory=tuple)
    brief_refs: tuple[str, ...] = field(default_factory=tuple)
    ownership_refs: tuple[str, ...] = field(default_factory=tuple)
    safety_refs: tuple[str, ...] = field(default_factory=tuple)
    proof_refs: tuple[str, ...] = field(default_factory=tuple)
    runtime_proof: dict[str, Any] | None = None


@dataclass(frozen=True)
class PolicyVerdict:
    """Result of :func:`evaluate_action`."""

    decision: Decision
    rule_id: str | None
    reason: str
    decision_id: str
    policy_digest: str
    override_required: bool

    @property
    def allowed(self) -> bool:
        return self.decision is Decision.ALLOW


@dataclass(frozen=True)
class PolicySource:
    """One loaded, digest-verified approved policy source."""

    version: int
    digest: str  # sha256:<hex> of rules.json bytes
    rules: tuple[Rule, ...]
    title: str
    decision_refs: tuple[str, ...]
    linear_issue: str
    effective_at: str


# ---------------------------------------------------------------------------
# Loading and digest verification
# ---------------------------------------------------------------------------


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise PolicyUnavailableError(message)


def _load_json_file(path: Path, what: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PolicyUnavailableError(f"{what} unreadable: {path}: {exc}") from exc


def _validate_rule(raw: Any) -> Rule:
    _require(isinstance(raw, dict), "policy rule must be an object")
    for key in ("rule_id", "title", "effect", "scope", "matcher", "deny_reason", "precedence"):
        _require(key in raw, f"rule {raw.get('rule_id', '<unknown>')} missing {key}")
    effect = str(raw["effect"])
    _require(effect in {"allow", "deny"}, f"rule {raw['rule_id']} effect must be allow/deny")
    override_raw = raw.get("override")
    override: OverridePolicy | None = None
    if override_raw is not None:
        _require(
            isinstance(override_raw, dict), f"rule {raw['rule_id']} override must be an object"
        )
        for key in ("requires_operator", "bound_action", "bound_scope", "expiry_seconds"):
            _require(key in override_raw, f"rule {raw['rule_id']} override missing {key}")
        override = OverridePolicy(
            requires_operator=str(override_raw["requires_operator"]),
            bound_action=str(override_raw["bound_action"]),
            bound_scope=str(override_raw["bound_scope"]),
            expiry_seconds=int(override_raw["expiry_seconds"]),
            nonce_required=bool(override_raw.get("nonce_required", True)),
            approval_ref=str(override_raw.get("approval_ref", "")),
        )
    matcher = raw["matcher"]
    _require(isinstance(matcher, dict), f"rule {raw['rule_id']} matcher must be an object")
    return Rule(
        rule_id=str(raw["rule_id"]),
        title=str(raw["title"]),
        effect=effect,
        scope=str(raw["scope"]),
        matcher=matcher,
        deny_reason=str(raw["deny_reason"]),
        override=override,
        waivable=bool(raw.get("waivable", False)),
        enabled=bool(raw.get("enabled", True)),
        precedence=int(raw["precedence"]),
        decision_refs=tuple(str(d) for d in raw.get("decision_refs", ())),
        linear_issue=str(raw.get("linear_issue", "")),
    )


def load_policy_source(policy_dir: Path | str | None = None) -> PolicySource:
    """Load and digest-verify the approved policy source.

    Fails closed with :class:`PolicyUnavailableError` when the source is
    missing, malformed, digest-mismatched, unsupported-versioned, or when any
    rule is malformed.  Memory retrieval is never consulted here.
    """
    directory = Path(policy_dir) if policy_dir is not None else DEFAULT_POLICY_DIR
    env_override = os.getenv(ENV_POLICY_DIR)
    if env_override and policy_dir is None:
        directory = Path(env_override)
    manifest_path = directory / MANIFEST_NAME
    rules_path = directory / RULES_NAME
    for path in (manifest_path, rules_path):
        _require(path.is_file(), f"operator policy source missing: {path}")

    manifest = _load_json_file(manifest_path, "operator policy manifest")
    _require(isinstance(manifest, dict), "operator policy manifest must be an object")
    _require(
        manifest.get("schema_version") == POLICY_SCHEMA_VERSION,
        f"unsupported policy schema {manifest.get('schema_version')!r}",
    )
    _require(
        manifest.get("version") == SUPPORTED_POLICY_VERSION,
        f"unsupported policy version {manifest.get('version')!r}",
    )
    manifest_digest = manifest.get("digest")
    _require(isinstance(manifest_digest, str), "operator policy manifest digest missing")
    _require(manifest_digest.startswith("sha256:"), "operator policy manifest digest missing")
    rules_bytes = rules_path.read_bytes()
    digest = "sha256:" + hashlib.sha256(rules_bytes).hexdigest()
    _require(
        digest == manifest_digest, "operator policy digest mismatch: rules altered since approval"
    )
    raw_rules = _load_json_file(rules_path, "operator policy rules")
    _require(isinstance(raw_rules, dict), "operator policy rules must be an object")
    rules_list = raw_rules.get("rules")
    _require(isinstance(rules_list, list), "operator policy rules list missing or empty")
    _require(len(rules_list) > 0, "operator policy rules list missing or empty")
    rule_ids: set[str] = set()
    rules: list[Rule] = []
    for raw in rules_list:
        rule = _validate_rule(raw)
        _require(rule.rule_id not in rule_ids, f"duplicate rule id {rule.rule_id}")
        rule_ids.add(rule.rule_id)
        rules.append(rule)
    return PolicySource(
        version=int(manifest["version"]),
        digest=digest,
        rules=tuple(rules),
        title=str(manifest.get("title", "")),
        decision_refs=tuple(str(d) for d in manifest.get("decision_refs", ())),
        linear_issue=str(manifest.get("linear_issue", "")),
        effective_at=str(manifest.get("effective_at", _iso_now())),
    )


# ---------------------------------------------------------------------------
# Canonicalization
# ---------------------------------------------------------------------------


def _canonical_path(value: str, cwd: Path) -> str:
    try:
        path = Path(value)
        if not path.is_absolute():
            path = cwd / path
        return str(path.resolve(strict=False))
    except (OSError, ValueError):
        return redact_text(str(value))


def _canonical_env(env: Mapping[str, str]) -> dict[str, str]:
    canon: dict[str, str] = {}
    for key, value in env.items():
        key_l = str(key).lower().replace(_LEGACY_ENV_PREFIX, _NEW_ENV_PREFIX, 1)
        canon[key_l] = str(value)
    return canon


@dataclass(frozen=True)
class _CanonContext:
    action: str
    argv0_basename: str
    argv_joined: str  # redacted; safe for matching and fingerprints
    env: dict[str, str]
    paths: frozenset[str]  # canonized absolute paths from argv/env/cwd/worktree
    cwd: str | None
    worktree: str | None
    actor: str | None
    linear_issue: str | None
    acceptance_criteria: frozenset[str]
    brief_refs: frozenset[str]
    ownership_refs: frozenset[str]
    safety_refs: frozenset[str]
    proof_refs: frozenset[str]


def _canonicalize(context: ActionContext) -> _CanonContext:
    cwd = Path(context.cwd) if context.cwd else Path.cwd()
    paths: list[str] = []
    if context.worktree:
        paths.append(_canonical_path(context.worktree, cwd))
    paths.append(_canonical_path(str(cwd), cwd))
    for item in context.argv:
        try:
            p = Path(item)
            if p.is_absolute() or ("/" in item and not item.startswith("-")):
                paths.append(_canonical_path(item, cwd))
        except (OSError, ValueError):
            pass
    for value in context.env.values():
        if value and (value.startswith("/") or value.startswith("~")):
            with contextlib.suppress(OSError, ValueError):
                paths.append(_canonical_path(value, cwd))
    return _CanonContext(
        action=context.action,
        argv0_basename=Path(context.argv[0]).name if context.argv else "",
        argv_joined=" ".join(redact_text(item) for item in context.argv),
        env=_canonical_env(context.env),
        paths=frozenset(paths),
        cwd=_canonical_path(str(cwd), cwd),
        worktree=_canonical_path(context.worktree, cwd) if context.worktree else None,
        actor=context.actor,
        linear_issue=context.linear_issue,
        acceptance_criteria=frozenset(context.acceptance_criteria),
        brief_refs=frozenset(context.brief_refs),
        ownership_refs=frozenset(context.ownership_refs),
        safety_refs=frozenset(context.safety_refs),
        proof_refs=frozenset(context.proof_refs),
    )


def _under(path: str, prefix: str) -> bool:
    if not path or not prefix:
        return False
    path = path if path.endswith("/") else path + "/"
    prefix = prefix if prefix.endswith("/") else prefix + "/"
    return path.startswith(prefix)


def _matcher_matches(rule: Rule, canon: _CanonContext) -> bool:
    matcher = rule.matcher
    if canon.action not in [str(a) for a in matcher.get("actions", ())]:
        return False
    command_names = matcher.get("command_names", ())
    if command_names and canon.argv0_basename not in [str(c) for c in command_names]:
        return False
    argv_joined_l = canon.argv_joined.lower()
    if matcher.get("argv_any") and not any(
        str(requirement).lower() in argv_joined_l for requirement in matcher["argv_any"]
    ):
        return False
    for exclusion in matcher.get("argv_excludes", ()):
        if str(exclusion).lower() in argv_joined_l:
            return False
    env_strings = [f"{key}={value}".lower() for key, value in canon.env.items()]
    if matcher.get("env_any") and not any(
        str(requirement).lower() in item
        for requirement in matcher["env_any"]
        for item in env_strings
    ):
        return False
    for exclusion in matcher.get("env_excludes", ()):
        excl = str(exclusion).lower()
        if any(excl in item for item in env_strings):
            return False
    if matcher.get("path_any") and not any(
        _under(path, _canonical_path(str(requirement), Path(canon.cwd or "/")))
        for requirement in matcher["path_any"]
        for path in canon.paths
    ):
        return False
    for exclusion in matcher.get("path_excludes", ()):
        prefix = _canonical_path(str(exclusion), Path(canon.cwd or "/"))
        if any(_under(path, prefix) for path in canon.paths):
            return False
    if matcher.get("requires_linear_issue") and not canon.linear_issue:
        return False
    if matcher.get("requires_acceptance_criteria") and not canon.acceptance_criteria:
        return False
    if matcher.get("requires_brief_refs") and not canon.brief_refs:
        return False
    if matcher.get("requires_ownership_refs") and not canon.ownership_refs:
        return False
    if matcher.get("requires_safety_refs") and not canon.safety_refs:
        return False
    if matcher.get("requires_proof_refs") and not canon.proof_refs:
        return False
    actor_requires = matcher.get("actor_requires", ())
    return not (actor_requires and canon.actor not in [str(a) for a in actor_requires])


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def _decision_id(policy_digest: str, rule_id: str | None, canon: _CanonContext) -> str:
    payload = {
        "policy_digest": policy_digest,
        "rule_id": rule_id,
        "action": canon.action,
        "actor": fingerprint_text(canon.actor) if canon.actor else None,
        "worktree": fingerprint_text(canon.worktree) if canon.worktree else None,
        "cwd": fingerprint_text(canon.cwd) if canon.cwd else None,
    }
    return "sha256:" + hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def evaluate_action(context: ActionContext, source: PolicySource) -> PolicyVerdict:
    """Evaluate one protected action against the approved policy source.

    First matching rule (by precedence) decides; when no rule matches the
    action is denied with ``no_rule_authorizes_action``.  A rule carrying an
    override policy denies pending a validated operator exception token
    (:func:`check_override_token`).
    """
    canon = _canonicalize(context)
    for rule in sorted(source.rules, key=lambda r: (r.precedence, r.rule_id)):
        if not rule.enabled:
            continue
        if not _matcher_matches(rule, canon):
            continue
        if rule.effect == "deny":
            if rule.override is not None:
                return PolicyVerdict(
                    decision=Decision.DENY,
                    rule_id=rule.rule_id,
                    reason=f"override_required:{rule.rule_id}:{rule.deny_reason}",
                    decision_id=_decision_id(source.digest, rule.rule_id, canon),
                    policy_digest=source.digest,
                    override_required=True,
                )
            return PolicyVerdict(
                decision=Decision.DENY,
                rule_id=rule.rule_id,
                reason=rule.deny_reason,
                decision_id=_decision_id(source.digest, rule.rule_id, canon),
                policy_digest=source.digest,
                override_required=False,
            )
        return PolicyVerdict(
            decision=Decision.ALLOW,
            rule_id=rule.rule_id,
            reason=f"rule:{rule.rule_id}",
            decision_id=_decision_id(source.digest, rule.rule_id, canon),
            policy_digest=source.digest,
            override_required=False,
        )
    return PolicyVerdict(
        decision=Decision.DENY,
        rule_id=None,
        reason="no_rule_authorizes_action",
        decision_id=_decision_id(source.digest, None, canon),
        policy_digest=source.digest,
        override_required=False,
    )


# ---------------------------------------------------------------------------
# Override tokens
# ---------------------------------------------------------------------------


def check_override_token(
    token: dict[str, Any],
    source: PolicySource,
    rule: Rule,
    *,
    nonce_store: Any,
    now: datetime | None = None,
) -> None:
    """Validate an approved exception token for *rule*.

    Raises :class:`OverrideRejectedError` on any mismatch: wrong operator,
    action/scope, expired or replayed nonce, digest mismatch.  A valid token
    is recorded as used (single-use) before returning.
    """
    if rule.override is None:
        raise OverrideRejectedError("rule carries no override policy")
    override = rule.override
    current = now or datetime.now(timezone.utc)
    policy_digest = token.get("policy_digest")
    if not isinstance(policy_digest, str) or policy_digest != source.digest:
        raise OverrideRejectedError("override_policy_digest_mismatch")
    if str(token.get("operator")) != override.requires_operator:
        raise OverrideRejectedError("override_operator_mismatch")
    if str(token.get("action")) != override.bound_action:
        raise OverrideRejectedError("override_action_mismatch")
    if str(token.get("scope")) != override.bound_scope:
        raise OverrideRejectedError("override_scope_mismatch")
    expiry_raw = token.get("expiry")
    try:
        expiry = datetime.fromisoformat(str(expiry_raw).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise OverrideRejectedError("override_expiry_unparseable") from exc
    if expiry.tzinfo is None:
        raise OverrideRejectedError("override_expiry_timezone_required")
    if expiry <= current:
        raise OverrideRejectedError("override_expired")
    nonce = token.get("nonce")
    if not isinstance(nonce, str) or not nonce.strip():
        raise OverrideRejectedError("override_nonce_required")
    if not (hasattr(nonce_store, "used") and hasattr(nonce_store, "record")):
        raise OverrideRejectedError("override_nonce_store_required")
    if nonce_store.used(nonce):
        raise OverrideRejectedError("override_replay_or_reused_nonce")
    nonce_store.record(nonce)


# ---------------------------------------------------------------------------
# Audit (secret-free)
# ---------------------------------------------------------------------------


def audit_record(
    verdict: PolicyVerdict,
    context: ActionContext,
    *,
    actor: str | None = None,
    timestamp: str | None = None,
) -> dict[str, Any]:
    """Build a secret-free audit record for one decision.

    Never includes raw argv, env, tokens, paths or PII; only fingerprints.
    """
    argv_text = " ".join(redact_text(item) for item in context.argv) if context.argv else None
    return {
        "decision_id": verdict.decision_id,
        "policy_digest": verdict.policy_digest,
        "rule_id": verdict.rule_id,
        "decision": verdict.decision.value,
        "reason": verdict.reason,
        "action": context.action,
        "override_required": verdict.override_required,
        "actor": fingerprint_text(actor or context.actor) if (actor or context.actor) else None,
        "worktree": fingerprint_text(context.worktree) if context.worktree else None,
        "argv_fingerprint": fingerprint_text(argv_text) if argv_text else None,
        "at": timestamp or _iso_now(),
    }


# ---------------------------------------------------------------------------
# Boundary admission (production caller for evaluate/override/audit)
# ---------------------------------------------------------------------------


class _MemoryNonceStore:
    """Process-local single-use nonce record. Not durable across restarts."""

    def __init__(self) -> None:
        self._used: set[str] = set()

    def used(self, nonce: str) -> bool:
        return nonce in self._used

    def record(self, nonce: str) -> None:
        self._used.add(nonce)


_NONCE_STORE = _MemoryNonceStore()


def admit_action(
    context: ActionContext,
    *,
    source: PolicySource | None = None,
    override_token: dict[str, Any] | None = None,
    nonce_store: Any | None = None,
    now: datetime | None = None,
) -> tuple[PolicyVerdict, dict[str, Any]]:
    """Evaluate one protected action and return the secret-free audit record.

    Loads the approved source (fail closed on missing/corrupt/stale policy),
    evaluates the action, and when a matching deny rule requires an override
    validates the supplied token through :func:`check_override_token`. A
    validated override is the only path that turns that deny into allow.
    Memory text is never consulted.
    """
    loaded = source if source is not None else load_policy_source()
    verdict = evaluate_action(context, loaded)
    if verdict.override_required and verdict.rule_id is not None and override_token is not None:
        rule = next(item for item in loaded.rules if item.rule_id == verdict.rule_id)
        store = nonce_store if nonce_store is not None else _NONCE_STORE
        check_override_token(override_token, loaded, rule, nonce_store=store, now=now)
        canon = _canonicalize(context)
        verdict = PolicyVerdict(
            decision=Decision.ALLOW,
            rule_id=verdict.rule_id,
            reason=f"override_accepted:{verdict.rule_id}",
            decision_id=_decision_id(loaded.digest, verdict.rule_id, canon),
            policy_digest=loaded.digest,
            override_required=False,
        )
    return verdict, audit_record(verdict, context)
