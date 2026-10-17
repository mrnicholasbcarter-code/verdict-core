"""Deterministic offline FakeMemoryProvider for unit/CI contract tests."""

from __future__ import annotations

from verdict_memory.contracts import (
    SHARED_MEMORY_PROTOCOL_VERSION,
    CertificationState,
    ExternalMemoryRef,
    LifecycleEvent,
    MemoryHit,
    MemoryProviderError,
    ProviderErrorCode,
    ProviderHealth,
    ProviderResultStatus,
    RecallQuery,
    RecallResult,
)


class FakeMemoryProvider:
    provider_id = "fake-verdict-memory"
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
        self._records: dict[str, LifecycleEvent] = {}

    def _check(self) -> None:
        if self._required_token is not None and self._token != self._required_token:
            raise MemoryProviderError(ProviderErrorCode.AUTH_FAILED, "authentication failed")
        if self.status == ProviderResultStatus.TIMEOUT:
            raise MemoryProviderError(ProviderErrorCode.TIMEOUT, "provider timed out")
        if self.status != ProviderResultStatus.AVAILABLE:
            raise MemoryProviderError(ProviderErrorCode.UNREACHABLE, "provider unavailable")

    def health(self) -> ProviderHealth:
        try:
            self._check()
        except MemoryProviderError as exc:
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

    def put(self, event: LifecycleEvent) -> ExternalMemoryRef:
        self._check()
        external_id = event.idempotency_key
        self._records.setdefault(external_id, event)
        return ExternalMemoryRef(self.provider_id, external_id, event.revision)

    def search(self, query: RecallQuery) -> RecallResult:
        try:
            self._check()
        except MemoryProviderError as exc:
            status = {
                ProviderErrorCode.TIMEOUT: ProviderResultStatus.TIMEOUT,
                ProviderErrorCode.AUTH_FAILED: ProviderResultStatus.AUTH_FAILED,
            }.get(exc.code, ProviderResultStatus.UNAVAILABLE)
            return RecallResult(status, error_code=exc.code, message=exc.safe_message)
        words = tuple(word.casefold() for word in query.query.split() if word)
        matches: list[MemoryHit] = []
        for external_id, event in self._records.items():
            if (
                event.identity.project != query.project
                or event.scope != query.scope
                or event.identity.tenant != query.tenant
            ):
                continue
            text = event.content.casefold()
            if words and not any(word in text for word in words):
                continue
            score = float(sum(word in text for word in words)) / max(1, len(words))
            matches.append(
                MemoryHit(
                    ExternalMemoryRef(self.provider_id, external_id, event.revision),
                    event,
                    score,
                    untrusted=True,
                )
            )
        matches.sort(key=lambda hit: (-hit.score, hit.ref.external_id))
        return RecallResult(ProviderResultStatus.AVAILABLE, tuple(matches[: query.limit]))

    def delete(self, ref: ExternalMemoryRef) -> bool:
        self._check()
        if ref.provider_id != self.provider_id:
            return False
        return self._records.pop(ref.external_id, None) is not None


__all__ = ["FakeMemoryProvider"]
