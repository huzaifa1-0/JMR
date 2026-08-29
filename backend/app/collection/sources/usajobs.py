"""USAJOBS -- the US federal job board.

Free API key, clean licensing, and no quota anxiety, which makes it the ideal
source to develop and test against.  Coverage is federal-government only, so it
is narrow but authoritative.
"""
from __future__ import annotations

import logging

from ..base import (BaseSource, Budget, CollectionQuery, RawPosting,
                    SourceCapabilities, SourceHealth)

log = logging.getLogger(__name__)

API = "https://data.usajobs.gov/api/search"
PER_PAGE = 100


class USAJobsSource(BaseSource):
    key = "usajobs"
    display_name = "USAJOBS (US federal)"
    kind = "government"
    capabilities = SourceCapabilities(
        keyword_search=True,
        location_search=True,
        salary_filter=True,
        remote_filter=True,
        full_description=True,
        countries=("US",),
    )

    def __init__(self, client, config=None):
        super().__init__(client, config)
        self.api_key = (self.config.get("api_key") or "").strip()
        self.email = (self.config.get("email") or "").strip()

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.email)

    def _headers(self) -> dict:
        return {"Host": "data.usajobs.gov",
                "User-Agent": self.email,
                "Authorization-Key": self.api_key}

    async def search(self, query: CollectionQuery,
                     budget: Budget) -> list[RawPosting]:
        if not self.configured:
            log.info("[usajobs] skipped -- no API key / email configured")
            return []

        params = {"ResultsPerPage": min(PER_PAGE, budget.remaining_results)}
        if query.query_text():
            params["Keyword"] = query.query_text()
        location = query.location_text()
        if location:
            params["LocationName"] = location
        if query.salary_min:
            params["RemunerationMinimumAmount"] = int(query.salary_min)
        if query.max_days_old:
            params["DatePosted"] = min(60, int(query.max_days_old))
        if query.remote_only:
            params["RemoteIndicator"] = "True"

        postings: list[RawPosting] = []
        page = 1
        while budget.can_call() and len(postings) < budget.remaining_results:
            budget.spend_call()
            params["Page"] = page
            data = await self.client.get_json(
                API, params=params, headers=self._headers(),
                source_key=self.key)
            if not data:
                break
            result = data.get("SearchResult") or {}
            items = result.get("SearchResultItems") or []
            if not items:
                break
            for item in items:
                posting = self._to_posting(item)
                if posting:
                    postings.append(posting)
            if len(items) < params["ResultsPerPage"]:
                break
            page += 1

        budget.add_results(len(postings))
        log.info("[usajobs] %d postings", len(postings))
        return postings

    def _to_posting(self, item: dict) -> RawPosting | None:
        d = item.get("MatchedObjectDescriptor") or {}
        if not d:
            return None

        locations = d.get("PositionLocation") or []
        first = locations[0] if locations else {}
        remuneration = (d.get("PositionRemuneration") or [{}])[0]
        detail = d.get("UserArea", {}).get("Details", {}) or {}

        body_parts = [
            detail.get("JobSummary"),
            detail.get("MajorDuties") if isinstance(detail.get("MajorDuties"), str)
            else "\n".join(detail.get("MajorDuties") or []),
            detail.get("Requirements"),
            detail.get("Qualifications"),
            detail.get("Education"),
            detail.get("Benefits"),
        ]
        description = "\n\n".join(p for p in body_parts if p)

        period_map = {"Per Year": "yearly", "Per Hour": "hourly",
                      "Per Month": "monthly", "Per Week": "weekly"}

        return RawPosting(
            source_key=self.key,
            source_job_id=str(d.get("PositionID") or item.get("MatchedObjectId")),
            title=self._clean(d.get("PositionTitle")) or "(untitled)",
            company_name=self._clean(d.get("OrganizationName")),
            description_text=description or None,
            description_html=None,
            location_raw=self._clean(first.get("LocationName")),
            city=self._clean(first.get("CityName")),
            state=self._clean(first.get("CountrySubDivisionCode")),
            country="US",
            latitude=first.get("Latitude"),
            longitude=first.get("Longitude"),
            is_remote=str(detail.get("TeleworkEligible", "")).lower() == "true",
            employment_type=self._clean(
                (d.get("PositionSchedule") or [{}])[0].get("Name")),
            salary_min=_to_float(remuneration.get("MinimumRange")),
            salary_max=_to_float(remuneration.get("MaximumRange")),
            salary_period=period_map.get(
                remuneration.get("RateIntervalCode"), "yearly"),
            salary_currency="USD",
            date_posted=d.get("PublicationStartDate"),
            job_url=self._clean(d.get("PositionURI")),
            apply_url=self._clean(
                (d.get("ApplyURI") or [None])[0] if d.get("ApplyURI") else None),
            raw={"grade": detail.get("LowGrade"),
                 "agency": d.get("DepartmentName")},
        )

    async def health(self) -> SourceHealth:
        if not self.configured:
            return SourceHealth(
                ok=False, configured=False,
                message="No API key / email. Register free at developer.usajobs.gov.")
        try:
            data = await self.client.get_json(
                API, params={"ResultsPerPage": 1},
                headers=self._headers(), source_key=self.key)
            return SourceHealth(ok=bool(data), message="Credentials accepted")
        except Exception as exc:                      # noqa: BLE001
            return SourceHealth(ok=False, message=str(exc))


def _to_float(value):
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None
