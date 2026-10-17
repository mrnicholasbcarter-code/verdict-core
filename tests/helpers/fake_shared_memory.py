"""Deterministic, offline ``SharedMemoryProvider`` used by the shared-memory tests.

Moved out of ``verdict/shared_memory.py`` (BOD-430 review): it has no production
caller, so it lives with the tests instead of carrying a reachability baseline entry.
"""

from __future__ import annotations

from verdict.shared_memory import (
    SHARED_MEMORY_PROTOCOL_VERSION,
    CertificationState,
    ExternalMemoryRef,
    ProviderErrorCode,
    ProviderHealth,
    ProviderResultStatus,
    SharedMemoryEnvelope,
    SharedMemoryHit,
    SharedMemoryProviderError,
    SharedMemoryQuery,
    SharedMemorySearchResult,
)


class FakeSharedMemoryProvider:
    """Deterministic, offline contract implementation used by CI and consumers."""

    provider_id = "fake-shared-memory"
    protocol_version = SHARED_MEMORY_PROTOCOL_VERSION

    def __init__(
        self,
        *,
        status: ProviderResultStatus = ProviderResultStatus.AVAILABLE,
        required_token: str | None = None,
        token: str | None = None,
    ) -> None:
        self.status = status
        self._required_token = required_token
        self._token = token
        self._records: dict[str, SharedMemoryEnvelope] = {}

    def _check(self) -> None:
        if self._required_token is not None and self._token != self._required_token:
            raise SharedMemoryProviderError(ProviderErrorCode.AUTH_FAILED, "authentication failed")
        if self.status == ProviderResultStatus.TIMEOUT:
            raise SharedMemoryProviderError(ProviderErrorCode.TIMEOUT, "provider timed out")
        if self.status != ProviderResultStatus.AVAILABLE:
            raise SharedMemoryProviderError(ProviderErrorCode.UNREACHABLE, "provider unavailable")

    def health(self) -> ProviderHealth:
        try:
            self._check()
        except SharedMemoryProviderError as exc:
            status = (
                ProviderResultStatus.AUTH_FAILED
                if exc.code == ProviderErrorCode.AUTH_FAILED
                else self.status
            )
            return ProviderHealth(
                self.provider_id,
                status,
                CertificationState.UNAVAILABLE,
                self.protocol_version,
                "fake-1",
                "memory",
                "failed" if exc.code == ProviderErrorCode.AUTH_FAILED else "not-required",
                exc.safe_message,
            )
        return ProviderHealth(
            self.provider_id,
            ProviderResultStatus.AVAILABLE,
            CertificationState.HEALTHY,
            self.protocol_version,
            "fake-1",
            "memory",
            "authenticated" if self._required_token else "not-required",
        )

    def put(self, envelope: SharedMemoryEnvelope) -> ExternalMemoryRef:
        self._check()
        external_id = envelope.idempotency_key
        self._records.setdefault(external_id, envelope)
        return ExternalMemoryRef(self.provider_id, external_id, envelope.revision)

    def search(self, query: SharedMemoryQuery) -> SharedMemorySearchResult:
        try:
            self._check()
        except SharedMemoryProviderError as exc:
            status = {
                ProviderErrorCode.TIMEOUT: ProviderResultStatus.TIMEOUT,
                ProviderErrorCode.AUTH_FAILED: ProviderResultStatus.AUTH_FAILED,
            }.get(exc.code, ProviderResultStatus.UNAVAILABLE)
            return SharedMemorySearchResult(status, error_code=exc.code, message=exc.safe_message)
        words = tuple(word.casefold() for word in query.query.split() if word)
        matches: list[SharedMemoryHit] = []
        for external_id, envelope in self._records.items():
            if (
                envelope.project != query.project
                or envelope.scope != query.scope
                or envelope.tenant != query.tenant
            ):
                continue
            text = envelope.content.casefold()
            if words and not any(word in text for word in words):
                continue
            score = float(sum(word in text for word in words)) / max(1, len(words))
            matches.append(
                SharedMemoryHit(
                    ExternalMemoryRef(self.provider_id, external_id, envelope.revision),
                    envelope,
                    score,
                )
            )
        matches.sort(key=lambda hit: (-hit.score, hit.ref.external_id))
        return SharedMemorySearchResult(
            ProviderResultStatus.AVAILABLE, tuple(matches[: query.limit])
        )

    def delete(self, ref: ExternalMemoryRef) -> bool:
        self._check()
        if ref.provider_id != self.provider_id:
            return False
        return self._records.pop(ref.external_id, None) is not None


__all__ = ["FakeSharedMemoryProvider"]
