"""Row schema for model-discovery source adapters."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DiscoveryRow:
    """A single fact row produced by one discovery source adapter.

    Adapters ONLY report facts they observe.  They never decide capacity.
    ``None`` means "not stated by this source".  ``free_flag`` is an
    explicit provider signal; a zero price is a separate fact recorded in
    ``input_price`` / ``output_price``.
    """

    # Identity
    id: str
    """Stable route/model ID as the source reports it (e.g. ``groq/llama-3.3-70b-versatile``)."""
    provider: str
    """Provider name (e.g. ``groq``, ``openrouter``, ``kilo``)."""

    # Provenance
    source: str
    """Short adapter name: ``omniroute``, ``openrouter``, ``kilo``,
    ``models_dev``, ``free_coding_models``, ``dsh_our_free_model``."""
    source_url: str
    """URL that was fetched to produce this row."""
    fetched_at: str
    """ISO 8601 UTC timestamp when this row was produced."""

    # Facts (None = not stated by this source)
    input_price: float | None = None
    """Input price per token in USD, or ``None`` if not stated."""
    output_price: float | None = None
    """Output price per token in USD, or ``None`` if not stated."""
    free_flag: bool | None = None
    """Provider's own ``isFree``-style flag, or ``None`` if absent."""
    context_length: int | None = None
    """Context window in tokens, or ``None`` if not stated."""
    supports_tools: bool | None = None
    """Whether tool/function calling is explicitly supported, or ``None``."""

    # Community-list provenance
    license: str | None = None
    """License SPDX ID or short description (community lists only)."""
    license_restricted: bool = False
    """``True`` when the source license does not permit data reuse."""
    commit: str | None = None
    """Git commit SHA of the fetched file (community lists only)."""
    repo: str | None = None
    """GitHub repo path, e.g. ``vava-nessa/free-coding-models`` (community lists only)."""

    def to_dict(self) -> dict[str, object]:
        """Return a plain-dict representation suitable for JSON serialisation."""
        return {
            "id": self.id,
            "provider": self.provider,
            "source": self.source,
            "source_url": self.source_url,
            "fetched_at": self.fetched_at,
            "input_price": self.input_price,
            "output_price": self.output_price,
            "free_flag": self.free_flag,
            "context_length": self.context_length,
            "supports_tools": self.supports_tools,
            "license": self.license,
            "license_restricted": self.license_restricted,
            "commit": self.commit,
            "repo": self.repo,
        }
