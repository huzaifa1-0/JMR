"""Adzuna adapter -- the breadth source.

Free tier is roughly 1,000 calls per month, so every call is spent
deliberately: 50 results per page, no speculative pagination past what the
budget allows.

Two known characteristics, both surfaced honestly to the user rather than
hidden:
  * descriptions are truncated (`description_is_truncated=True`), which is why
    the ATS discovery loop exists
  * many salaries are Adzuna's own estimates, flagged via `salary_is_estimated`
"""
from __future__ import annotations

import logging

from ..base import (BaseSource, Budget, CollectionQuery, RawPosting,
                    SourceCapabilities, SourceHealth)

log = logging.getLogger(__name__)

BASE = "https://api.adzuna.com/v1/api/jobs"
MAX_PER_PAGE = 50


class AdzunaSource(BaseSource):
    key = "adzuna"
    display_name = "Adzuna"
    kind = "aggregator"
    capabilities = SourceCapabilities(
        keyword_search=True,
        location_search=True,
        salary_filter=True,
        date_filter=True,
        employment_type_filter=True,
        full_description=False,
        countries=("US", "GB", "CA", "AU", "DE", "FR", "IN"),
    )

    def __init__(self, client, config=None):
        super().__init__(client, config)
        self.app_id = (self.config.get("app_id") or "").strip()
        self.app_key = (self.config.get("app_key") or "").strip()

    @property
    def configured(self) -> bool:
        return bool(self.app_id and self.app_key)

    def _auth(self) -> dict:
        return {"app_id": self.app_id, "app_key": self.app_key}

    async def search(self, query: CollectionQuery,
                     budget: Budget) -> list[RawPosting]:
        if not self.configured:
            log.info("[adzuna] skipped -- no credentials configured")
            return []

        country = (query.country or "us").lower()
        params = self._auth() | {
            "results_per_page": min(MAX_PER_PAGE, budget.remaining_results),
            "content-type": "application/json",
        }
        if query.query_text():
            params["what"] = query.query_text()
        if query.company:
            params["company"] = query.company
        location = query.location_text()
        if location:
            params["where"] = location
        if query.max_days_old:
            params["max_days_old"] = int(query.max_days_old)
        if query.salary_min:
            params["salary_min"] = int(query.salary_min)
        if query.employment_type == "full_time":
            params["full_time"] = 1
        elif query.employment_type == "part_time":
            params["part_time"] = 1
        elif query.employment_type == "contract":
            params["contract"] = 1

        postings: list[RawPosting] = []
        page = 1
        while budget.can_call() and len(postings) < budget.remaining_results:
            budget.spend_call()
            data = await self.client.get_json(
                f"{BASE}/{country}/search/{page}", params=params,
                source_key=self.key)
            if not data:
                break
            results = data.get("results") or []
            if not results:
                break
            for item in results:
                postings.append(self._to_posting(item))
            if len(results) < params["results_per_page"]:
                break
            page += 1

        budget.add_results(len(postings))
        log.info("[adzuna] %d postings across %d page(s)", len(postings), page)
        return postings

    def _to_posting(self, item: dict) -> RawPosting:
        company = (item.get("company") or {}).get("display_name")
        loc = item.get("location") or {}
        areas = loc.get("area") or []
        # area is ordered broadest-first: ["US", "California", "San Francisco"]
        state = areas[1] if len(areas) > 1 else None
        city = areas[-1] if len(areas) > 2 else None

        salary_min = item.get("salary_min")
        salary_max = item.get("salary_max")
        is_predicted = str(item.get("salary_is_predicted", "0")) == "1"

        return RawPosting(
            source_key=self.key,
            source_job_id=str(item.get("id")),
            title=self._clean(item.get("title")) or "(untitled)",
            company_name=self._clean(company),
            description_html=item.get("description"),
            description_is_truncated=True,
            location_raw=self._clean(loc.get("display_name")),
            city=self._clean(city),
            state=self._clean(state),
            country="US",
            latitude=item.get("latitude"),
            longitude=item.get("longitude"),
            employment_type=self._clean(item.get("contract_time")),
            salary_min=salary_min,
            salary_max=salary_max,
            salary_period="yearly",
            salary_is_estimated=is_predicted,
            date_posted=item.get("created"),
            job_url=self._clean(item.get("redirect_url")),
            apply_url=self._clean(item.get("redirect_url")),
            raw={"category": (item.get("category") or {}).get("label"),
                 "contract_type": item.get("contract_type")},
        )

    # -- market analytics (spec section 15) ------------------------------
    async def salary_histogram(self, what: str, where: str | None = None,
                               country: str = "us") -> dict | None:
        if not self.configured:
            return None
        params = self._auth() | {"what": what, "content-type": "application/json"}
        if where:
            params["location0"] = where
        return await self.client.get_json(
            f"{BASE}/{country}/histogram", params=params, source_key=self.key)

    async def top_companies(self, what: str, country: str = "us") -> dict | None:
        if not self.configured:
            return None
        params = self._auth() | {"what": what, "content-type": "application/json"}
        return await self.client.get_json(
            f"{BASE}/{country}/top_companies", params=params,
            source_key=self.key)

    async def health(self) -> SourceHealth:
        if not self.configured:
            return SourceHealth(
                ok=False, configured=False,
                message="No App ID / App Key. Register free at developer.adzuna.com.")
        try:
            data = await self.client.get_json(
                f"{BASE}/us/search/1",
                params=self._auth() | {"results_per_page": 1,
                                       "content-type": "application/json"},
                source_key=self.key)
            return SourceHealth(ok=bool(data), message="Credentials accepted")
        except Exception as exc:                      # noqa: BLE001
            return SourceHealth(ok=False, message=str(exc))
