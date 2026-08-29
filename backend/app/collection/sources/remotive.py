"""Remotive -- free public remote-jobs feed, no credentials required.

Included so the application produces real data on first launch, before the user
has registered for anything.  Coverage is remote-only and tech-leaning, so treat
it as a starting corpus rather than a representative sample of the US market.
"""
from __future__ import annotations

import logging

from ..base import BaseSource, Budget, CollectionQuery, RawPosting, SourceCapabilities, SourceHealth

log = logging.getLogger(__name__)

API = "https://remotive.com/api/remote-jobs"


class RemotiveSource(BaseSource):
    key = "remotive"
    display_name = "Remotive (remote jobs)"
    kind = "aggregator"
    capabilities = SourceCapabilities(
        keyword_search=True,
        remote_filter=True,
        full_description=True,
        countries=("US", "GLOBAL"),
    )

    async def search(self, query: CollectionQuery,
                     budget: Budget) -> list[RawPosting]:
        if not budget.can_call():
            return []

        params = {"limit": min(budget.remaining_results, 200)}
        search_text = query.query_text()
        if search_text:
            params["search"] = search_text

        budget.spend_call()
        data = await self.client.get_json(API, params=params,
                                          source_key=self.key)
        if not data:
            return []

        postings: list[RawPosting] = []
        for item in (data.get("jobs") or [])[:budget.remaining_results]:
            postings.append(self._to_posting(item))
        budget.add_results(len(postings))
        log.info("[remotive] %d postings", len(postings))
        return postings

    def _to_posting(self, item: dict) -> RawPosting:
        salary_raw = self._clean(item.get("salary"))
        return RawPosting(
            source_key=self.key,
            source_job_id=str(item.get("id")),
            title=self._clean(item.get("title")) or "(untitled)",
            company_name=self._clean(item.get("company_name")),
            company_website=self._clean(item.get("company_logo")),
            description_html=item.get("description"),
            location_raw=self._clean(item.get("candidate_required_location")),
            country="US",
            is_remote=True,
            employment_type=self._clean(item.get("job_type")),
            date_posted=item.get("publication_date"),
            job_url=self._clean(item.get("url")),
            apply_url=self._clean(item.get("url")),
            raw={"salary_text": salary_raw, "category": item.get("category"),
                 "tags": item.get("tags")},
        )

    async def health(self) -> SourceHealth:
        try:
            data = await self.client.get_json(API, params={"limit": 1},
                                              source_key=self.key)
            return SourceHealth(ok=bool(data), message="Reachable")
        except Exception as exc:                      # noqa: BLE001
            return SourceHealth(ok=False, message=str(exc))
