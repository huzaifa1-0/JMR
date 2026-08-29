"""Concrete ATS adapters: Greenhouse, Lever, Ashby, Workable.

All four expose public, unauthenticated JSON board endpoints.  Each vendor's
terms should be read once before large-scale use; the adapter records the
endpoint it used so provenance is always traceable from the job record.
"""
from __future__ import annotations

import logging

from ..base import RawPosting, SourceHealth
from .ats_base import AtsSource

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
class GreenhouseSource(AtsSource):
    key = "greenhouse"
    display_name = "Greenhouse boards"

    def board_url(self, token: str) -> str:
        return (f"https://boards-api.greenhouse.io/v1/boards/{token}"
                f"/jobs?content=true")

    def parse_board(self, data, token, company_name):
        out = []
        for item in (data.get("jobs") or []):
            loc = (item.get("location") or {}).get("name")
            out.append(RawPosting(
                source_key=self.key,
                source_job_id=f"{token}:{item.get('id')}",
                title=self._clean(item.get("title")) or "(untitled)",
                company_name=company_name or token,
                description_html=item.get("content"),
                location_raw=self._clean(loc),
                date_posted=item.get("updated_at") or item.get("first_published"),
                job_url=self._clean(item.get("absolute_url")),
                apply_url=self._clean(item.get("absolute_url")),
                raw={"board_token": token,
                     "departments": [d.get("name") for d in
                                     (item.get("departments") or [])],
                     "offices": [o.get("name") for o in
                                 (item.get("offices") or [])]},
            ))
        return out

    async def health(self) -> SourceHealth:
        return SourceHealth(ok=True,
                            message="Public endpoint, no credentials needed")


# --------------------------------------------------------------------------
class LeverSource(AtsSource):
    key = "lever"
    display_name = "Lever boards"

    def board_url(self, token: str) -> str:
        return f"https://api.lever.co/v0/postings/{token}?mode=json"

    def parse_board(self, data, token, company_name):
        if not isinstance(data, list):
            return []
        out = []
        for item in data:
            categories = item.get("categories") or {}
            out.append(RawPosting(
                source_key=self.key,
                source_job_id=f"{token}:{item.get('id')}",
                title=self._clean(item.get("text")) or "(untitled)",
                company_name=company_name or token,
                description_html=item.get("description")
                or item.get("descriptionPlain"),
                location_raw=self._clean(categories.get("location")),
                employment_type=self._clean(categories.get("commitment")),
                date_posted=item.get("createdAt"),
                job_url=self._clean(item.get("hostedUrl")),
                apply_url=self._clean(item.get("applyUrl")
                                      or item.get("hostedUrl")),
                raw={"board_token": token,
                     "team": categories.get("team"),
                     "department": categories.get("department"),
                     "workplace_type": item.get("workplaceType"),
                     "lists": item.get("lists")},
            ))
        return out

    async def health(self) -> SourceHealth:
        return SourceHealth(ok=True,
                            message="Public endpoint, no credentials needed")


# --------------------------------------------------------------------------
class AshbySource(AtsSource):
    key = "ashby"
    display_name = "Ashby boards"

    def board_url(self, token: str) -> str:
        return (f"https://api.ashbyhq.com/posting-api/job-board/{token}"
                f"?includeCompensation=true")

    def parse_board(self, data, token, company_name):
        out = []
        for item in (data.get("jobs") or []):
            comp = item.get("compensation") or {}
            summary = comp.get("compensationTierSummary")
            out.append(RawPosting(
                source_key=self.key,
                source_job_id=f"{token}:{item.get('id')}",
                title=self._clean(item.get("title")) or "(untitled)",
                company_name=company_name or token,
                description_html=item.get("descriptionHtml"),
                description_text=item.get("descriptionPlain"),
                location_raw=self._clean(item.get("location")),
                employment_type=self._clean(item.get("employmentType")),
                is_remote=item.get("isRemote"),
                date_posted=item.get("publishedAt") or item.get("updatedAt"),
                job_url=self._clean(item.get("jobUrl")),
                apply_url=self._clean(item.get("applyUrl")
                                      or item.get("jobUrl")),
                raw={"board_token": token,
                     "department": item.get("department"),
                     "team": item.get("team"),
                     "compensation_summary": summary},
            ))
        return out

    async def health(self) -> SourceHealth:
        return SourceHealth(ok=True,
                            message="Public endpoint, no credentials needed")


# --------------------------------------------------------------------------
class WorkableSource(AtsSource):
    key = "workable"
    display_name = "Workable boards"

    def board_url(self, token: str) -> str:
        return f"https://apply.workable.com/api/v1/widget/accounts/{token}"

    def parse_board(self, data, token, company_name):
        out = []
        name = (data.get("name") if isinstance(data, dict) else None) \
            or company_name or token
        for item in (data.get("jobs") or []):
            city = self._clean(item.get("city"))
            state = self._clean(item.get("state"))
            country = self._clean(item.get("country")) or "US"
            loc = ", ".join(p for p in (city, state) if p) or None
            out.append(RawPosting(
                source_key=self.key,
                source_job_id=f"{token}:{item.get('shortcode') or item.get('id')}",
                title=self._clean(item.get("title")) or "(untitled)",
                company_name=name,
                description_html=item.get("description"),
                location_raw=loc,
                city=city,
                state=state,
                country="US" if country.upper() in ("US", "USA",
                                                    "UNITED STATES") else country,
                employment_type=self._clean(item.get("employment_type")),
                is_remote=bool(item.get("telecommuting")),
                date_posted=item.get("published_on") or item.get("created_at"),
                job_url=self._clean(item.get("url") or item.get("application_url")),
                apply_url=self._clean(item.get("application_url")
                                      or item.get("url")),
                raw={"board_token": token,
                     "department": item.get("department")},
            ))
        return out

    async def health(self) -> SourceHealth:
        return SourceHealth(ok=True,
                            message="Public endpoint, no credentials needed")
