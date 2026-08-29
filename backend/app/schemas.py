"""Pydantic request/response models.

Validation lives here, which is what makes most of the spec's section 24 error
handling automatic: a bad filter produces a precise 422 rather than a traceback.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

WorkArrangement = Literal["remote", "hybrid", "onsite", "unknown"]
EmploymentType = Literal["full_time", "part_time", "contract", "temporary",
                         "internship"]
ExperienceLevel = Literal["entry", "mid", "senior", "manager", "director",
                          "executive"]
ResearchStatus = Literal["new", "reviewed", "relevant", "not_relevant",
                         "follow_up", "archived"]
Priority = Literal["high", "medium", "low"]
FieldType = Literal["text", "longtext", "number", "boolean", "date",
                    "dropdown", "multiselect", "score"]


# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------
class CustomFieldFilter(BaseModel):
    key: str
    op: str = "eq"
    value: Any = None


class SearchQuery(BaseModel):
    # basic -- what
    job_title: str | None = None
    keywords: str | None = None
    skills_text: str | None = None
    company: str | None = None
    boolean: str | None = None

    # basic -- where
    city: str | None = None
    state: str | None = None
    country: str | None = None
    postal_code: str | None = None

    # job filters
    date_posted: str | None = None
    date_from: str | None = None
    date_to: str | None = None
    employment_types: list[EmploymentType] = Field(default_factory=list)
    work_arrangements: list[WorkArrangement] = Field(default_factory=list)
    experience_levels: list[ExperienceLevel] = Field(default_factory=list)
    salary_min: float | None = None
    salary_max: float | None = None
    has_salary: bool = False
    employer_stated_salary: bool = False

    # advanced job research
    title_contains: str | None = None
    title_not_contains: str | None = None
    description_contains: str | None = None
    description_not_contains: str | None = None
    required_skills: list[str] = Field(default_factory=list)
    any_skills: list[str] = Field(default_factory=list)
    exclude_skills: list[str] = Field(default_factory=list)
    education_levels: list[str] = Field(default_factory=list)
    years_experience_min: int | None = None
    years_experience_max: int | None = None
    visa_sponsorship: bool | None = None
    relocation: bool | None = None
    urgently_hiring: bool | None = None
    easy_apply: bool | None = None
    max_posting_age_days: int | None = None
    min_score: float | None = None

    # company research
    company_industry: str | None = None
    company_domain: str | None = None
    company_size_min: int | None = None
    company_size_max: int | None = None
    company_min_jobs: int | None = None
    company_min_hiring_frequency: float | None = None
    company_ats: list[str] = Field(default_factory=list)

    # research layer
    research_status: list[ResearchStatus] = Field(default_factory=list)
    priority: list[Priority] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    custom_fields: list[CustomFieldFilter] = Field(default_factory=list)

    # corpus / sources
    sources: list[str] = Field(default_factory=list)
    include_duplicates: bool = False
    include_inactive: bool = False

    # presentation
    sort: str = "relevance"
    sort_dir: Literal["asc", "desc"] = "desc"
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=50, ge=1, le=500)

    @field_validator("salary_min", "salary_max")
    @classmethod
    def _non_negative(cls, v):
        if v is not None and v < 0:
            raise ValueError("Salary cannot be negative.")
        return v


class SkillOut(BaseModel):
    name: str
    requirement: str
    mention_count: int


class ResearchOut(BaseModel):
    status: str = "new"
    priority: str | None = None
    lead_quality: int | None = None
    notes: str | None = None
    follow_up_at: datetime | None = None
    tags: list[str] = Field(default_factory=list)


class JobOut(BaseModel):
    id: int
    title: str
    company: str | None = None
    company_id: int | None = None
    location: str | None = None
    city: str | None = None
    state: str | None = None
    work_arrangement: str | None = None
    employment_type: str | None = None
    experience_level: str | None = None
    education_level: str | None = None
    years_experience_min: int | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    salary_period: str | None = None
    salary_annualized_min: float | None = None
    salary_is_estimated: bool = False
    date_posted: datetime | None = None
    date_collected: datetime | None = None
    job_url: str | None = None
    source: str | None = None
    opportunity_score: float | None = None
    visa_sponsorship_mentioned: bool | None = None
    relocation_mentioned: bool | None = None
    urgently_hiring: bool | None = None
    description_summary: str | None = None
    description_is_truncated: bool = False
    duplicate_count: int = 0
    skills: list[SkillOut] = Field(default_factory=list)
    research: ResearchOut | None = None


class SearchResponse(BaseModel):
    total: int
    page: int
    page_size: int
    pages: int
    results: list[JobOut]
    facets: dict | None = None
    query_echo: dict | None = None


# --------------------------------------------------------------------------
# Research
# --------------------------------------------------------------------------
class ResearchUpdate(BaseModel):
    status: ResearchStatus | None = None
    priority: Priority | None = None
    lead_quality: int | None = Field(default=None, ge=1, le=5)
    notes: str | None = None
    follow_up_at: datetime | None = None
    tags: list[str] | None = None


class BulkResearchUpdate(BaseModel):
    job_ids: list[int] = Field(min_length=1)
    status: ResearchStatus | None = None
    priority: Priority | None = None
    add_tags: list[str] = Field(default_factory=list)
    remove_tags: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Custom fields
# --------------------------------------------------------------------------
class CustomFieldCreate(BaseModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,49}$")
    label: str
    entity: Literal["job", "company"] = "job"
    field_type: FieldType
    options: list[str] = Field(default_factory=list)
    default_value: str | None = None
    is_filterable: bool = True
    sort_order: int = 0


class CustomFieldOut(BaseModel):
    id: int
    key: str
    label: str
    entity: str
    field_type: str
    options: list[str] = Field(default_factory=list)
    default_value: str | None = None
    is_filterable: bool = True
    sort_order: int = 0


class CustomFieldValueSet(BaseModel):
    entity: Literal["job", "company"] = "job"
    entity_id: int
    value: Any = None


# --------------------------------------------------------------------------
# Saved searches / collection
# --------------------------------------------------------------------------
class SavedSearchCreate(BaseModel):
    name: str
    description: str | None = None
    query: SearchQuery


class CollectionRequest(BaseModel):
    job_title: str | None = None
    keywords: str | None = None
    company: str | None = None
    location: str | None = None
    city: str | None = None
    state: str | None = None
    country: str = "us"
    postal_code: str | None = None
    remote_only: bool = False
    max_days_old: int | None = Field(default=None, ge=1, le=365)
    salary_min: float | None = None
    employment_type: EmploymentType | None = None
    sources: list[str] = Field(default_factory=list)
    name: str | None = None


# --------------------------------------------------------------------------
# Export / settings
# --------------------------------------------------------------------------
class ExportRequest(BaseModel):
    format: Literal["csv", "xlsx", "json"] = "csv"
    query: SearchQuery
    include_description: bool = False
    include_custom_fields: bool = True
    filename: str | None = None
    max_rows: int = Field(default=50000, ge=1, le=200000)


class SettingUpdate(BaseModel):
    values: dict[str, Any]


class ScoringRuleIn(BaseModel):
    id: int | None = None
    name: str
    entity: Literal["job", "company"] = "job"
    is_active: bool = True
    weight: float = 1.0
    points: float = 10.0
    condition: dict
