"""The collection contract.

Everything above this layer speaks `RawPosting`.  Everything below it speaks
whatever the provider happens to speak.  Swapping a data source means writing
one class that passes the adapter contract tests -- nothing else changes.

Compliance rules enforced structurally in this layer:
  * no CAPTCHA handling, no headless browsers, no fingerprint spoofing
  * no proxy or identity rotation
  * no authenticated or private endpoints
  * quotas and rate limits are enforced in code, not merely documented
  * an honest, contactable User-Agent on every request
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Protocol, runtime_checkable

log = logging.getLogger(__name__)


@dataclass
class RawPosting:
    """One posting, as close to the source's own words as possible.

    Normalisation happens later, in the processing layer -- keeping this dumb
    means an adapter is easy to write and easy to test.
    """
    source_key: str
    source_job_id: str
    title: str

    company_name: str | None = None
    company_domain: str | None = None
    company_website: str | None = None

    description_html: str | None = None
    description_text: str | None = None
    description_is_truncated: bool = False

    location_raw: str | None = None
    city: str | None = None
    state: str | None = None
    country: str | None = "US"
    postal_code: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    is_remote: bool | None = None

    employment_type: str | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    salary_period: str | None = None
    salary_currency: str | None = "USD"
    salary_is_estimated: bool = False

    date_posted: datetime | None = None
    job_url: str | None = None
    apply_url: str | None = None

    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class SourceCapabilities:
    """What the provider can filter server-side.

    Anything not listed is applied locally after ingest -- which is why the
    local corpus, not the provider, is the thing the UI searches.
    """
    keyword_search: bool = False
    location_search: bool = False
    salary_filter: bool = False
    date_filter: bool = False
    employment_type_filter: bool = False
    remote_filter: bool = False
    full_description: bool = False
    requires_company_token: bool = False
    countries: tuple[str, ...] = ("US",)


@dataclass
class Budget:
    """A hard ceiling on what one collection run may spend."""
    max_calls: int = 20
    max_results: int = 500
    calls_used: int = 0
    results_collected: int = 0

    def can_call(self) -> bool:
        return (self.calls_used < self.max_calls
                and self.results_collected < self.max_results)

    def spend_call(self) -> None:
        self.calls_used += 1

    def add_results(self, n: int) -> None:
        self.results_collected += n

    @property
    def remaining_results(self) -> int:
        return max(0, self.max_results - self.results_collected)


@dataclass
class SourceHealth:
    ok: bool
    message: str = ""
    configured: bool = True


@dataclass
class CollectionQuery:
    """Provider-agnostic description of what to collect."""
    keywords: str | None = None
    job_title: str | None = None
    company: str | None = None
    location: str | None = None
    city: str | None = None
    state: str | None = None
    country: str = "us"
    postal_code: str | None = None
    remote_only: bool = False
    max_days_old: int | None = None
    salary_min: float | None = None
    employment_type: str | None = None
    company_tokens: list[tuple[str, str]] = field(default_factory=list)
    # ^ [(board_token, company_name)] for ATS sources

    def query_text(self) -> str:
        return " ".join(filter(None, [self.job_title, self.keywords])).strip()

    def location_text(self) -> str | None:
        if self.location:
            return self.location
        parts = [p for p in (self.city, self.state) if p]
        return ", ".join(parts) if parts else None


@runtime_checkable
class JobSource(Protocol):
    key: str
    display_name: str
    kind: str
    capabilities: SourceCapabilities

    async def search(self, query: CollectionQuery,
                     budget: Budget) -> Iterable[RawPosting]:
        ...

    async def health(self) -> SourceHealth:
        ...


class BaseSource:
    """Shared plumbing. Adapters subclass this and implement `search`."""

    key: str = "base"
    display_name: str = "Base"
    kind: str = "aggregator"
    capabilities = SourceCapabilities()

    def __init__(self, client, config: dict | None = None):
        self.client = client          # collection.http.HttpClient
        self.config = config or {}

    async def search(self, query: CollectionQuery,
                     budget: Budget) -> list[RawPosting]:
        raise NotImplementedError

    async def health(self) -> SourceHealth:
        return SourceHealth(ok=True)

    # -- helpers for subclasses ----------------------------------------
    @staticmethod
    def _clean(value) -> str | None:
        if value is None:
            return None
        s = str(value).strip()
        return s or None
