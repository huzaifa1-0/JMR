"""SQLAlchemy models -- the schema defined in section 4 of the architecture doc."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, Column, DateTime, Float, ForeignKey, Index, Integer, String,
    Text, UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------
class Source(Base):
    __tablename__ = "source"

    id = Column(Integer, primary_key=True)
    key = Column(String, unique=True, nullable=False)
    display_name = Column(String, nullable=False)
    kind = Column(String)                    # aggregator | ats | government
    enabled = Column(Boolean, default=True)
    requires_credentials = Column(Boolean, default=False)
    config_json = Column(Text)

    monthly_quota = Column(Integer)          # NULL = unlimited
    calls_used_this_period = Column(Integer, default=0)
    period_reset_at = Column(DateTime)

    last_success_at = Column(DateTime)
    last_error = Column(Text)
    health = Column(String, default="unknown")   # ok|degraded|down|quota_exhausted|not_configured

    jobs = relationship("Job", back_populates="source")


# --------------------------------------------------------------------------
# Company
# --------------------------------------------------------------------------
class Company(Base):
    __tablename__ = "company"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    name_normalized = Column(String, nullable=False, index=True)
    domain = Column(String)
    website = Column(String)
    industry = Column(String)
    size_bucket = Column(String)
    size_estimate = Column(Integer)
    hq_location = Column(String)

    ats_platform = Column(String)            # greenhouse|lever|ashby|workable|recruitee
    ats_board_token = Column(String)

    jobs_discovered = Column(Integer, default=0)
    first_seen_at = Column(DateTime, default=utcnow)
    last_seen_at = Column(DateTime, default=utcnow)
    hiring_frequency_30d = Column(Float, default=0.0)
    opportunity_score = Column(Float)
    research_notes = Column(Text)

    jobs = relationship("Job", back_populates="company")

    __table_args__ = (
        UniqueConstraint("name_normalized", "domain", name="uq_company_ident"),
        Index("idx_company_ats", "ats_platform", "ats_board_token"),
    )


# --------------------------------------------------------------------------
# Job
# --------------------------------------------------------------------------
class Job(Base):
    __tablename__ = "job"

    id = Column(Integer, primary_key=True)
    source_id = Column(Integer, ForeignKey("source.id"))
    source_job_id = Column(String)
    job_url = Column(Text)
    apply_url = Column(Text)

    company_id = Column(Integer, ForeignKey("company.id"))
    company_name_raw = Column(String)

    title = Column(String, nullable=False)
    title_normalized = Column(String, index=True)
    description_html = Column(Text)
    description_text = Column(Text)
    description_is_truncated = Column(Boolean, default=False)

    location_raw = Column(String)
    city = Column(String)
    state = Column(String)
    country = Column(String, default="US")
    postal_code = Column(String)
    latitude = Column(Float)
    longitude = Column(Float)

    work_arrangement = Column(String, default="unknown")   # remote|hybrid|onsite|unknown
    employment_type = Column(String)
    experience_level = Column(String)
    years_experience_min = Column(Integer)
    years_experience_max = Column(Integer)
    education_level = Column(String)

    salary_min = Column(Float)
    salary_max = Column(Float)
    salary_currency = Column(String, default="USD")
    salary_period = Column(String)
    salary_annualized_min = Column(Float, index=True)
    salary_annualized_max = Column(Float)
    salary_is_estimated = Column(Boolean, default=False)

    visa_sponsorship_mentioned = Column(Boolean)
    relocation_mentioned = Column(Boolean)
    urgently_hiring = Column(Boolean)
    easy_apply = Column(Boolean)

    date_posted = Column(DateTime, index=True)
    date_collected = Column(DateTime, default=utcnow, nullable=False)
    last_seen_at = Column(DateTime, default=utcnow)
    is_active = Column(Boolean, default=True)

    canonical_hash = Column(String, index=True)
    simhash = Column(String)
    duplicate_of_id = Column(Integer, ForeignKey("job.id"))
    is_primary = Column(Boolean, default=True)
    duplicate_count = Column(Integer, default=0)

    opportunity_score = Column(Float)
    raw_json = Column(Text)

    source = relationship("Source", back_populates="jobs")
    company = relationship("Company", back_populates="jobs")
    skills = relationship("JobSkill", back_populates="job",
                          cascade="all, delete-orphan")
    research = relationship("Research", back_populates="job", uselist=False,
                            cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("source_id", "source_job_id", name="uq_job_source"),
        Index("idx_job_active", "is_active", "is_primary"),
    )


# --------------------------------------------------------------------------
# Skills / benefits
# --------------------------------------------------------------------------
class Skill(Base):
    __tablename__ = "skill"

    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, nullable=False)
    category = Column(String)
    aliases_json = Column(Text)

    jobs = relationship("JobSkill", back_populates="skill")


class JobSkill(Base):
    __tablename__ = "job_skill"

    job_id = Column(Integer, ForeignKey("job.id", ondelete="CASCADE"),
                    primary_key=True)
    skill_id = Column(Integer, ForeignKey("skill.id"), primary_key=True)
    requirement = Column(String, default="mentioned")   # required|preferred|mentioned
    mention_count = Column(Integer, default=1)

    job = relationship("Job", back_populates="skills")
    skill = relationship("Skill", back_populates="jobs")


class JobBenefit(Base):
    __tablename__ = "job_benefit"

    job_id = Column(Integer, ForeignKey("job.id", ondelete="CASCADE"),
                    primary_key=True)
    benefit = Column(String, primary_key=True)


# --------------------------------------------------------------------------
# Research layer
# --------------------------------------------------------------------------
class Research(Base):
    __tablename__ = "research"

    id = Column(Integer, primary_key=True)
    job_id = Column(Integer, ForeignKey("job.id", ondelete="CASCADE"),
                    unique=True)
    status = Column(String, default="new")
    priority = Column(String)
    lead_quality = Column(Integer)
    notes = Column(Text)
    follow_up_at = Column(DateTime)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)

    job = relationship("Job", back_populates="research")


class Tag(Base):
    __tablename__ = "tag"
    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, nullable=False)
    color = Column(String, default="#64748b")


class JobTag(Base):
    __tablename__ = "job_tag"
    job_id = Column(Integer, ForeignKey("job.id", ondelete="CASCADE"),
                    primary_key=True)
    tag_id = Column(Integer, ForeignKey("tag.id", ondelete="CASCADE"),
                    primary_key=True)


# --------------------------------------------------------------------------
# Custom fields (EAV with typed value columns so they stay filterable)
# --------------------------------------------------------------------------
class CustomField(Base):
    __tablename__ = "custom_field"

    id = Column(Integer, primary_key=True)
    key = Column(String, unique=True, nullable=False)
    label = Column(String, nullable=False)
    entity = Column(String, nullable=False, default="job")
    field_type = Column(String, nullable=False)
    options_json = Column(Text)
    default_value = Column(Text)
    is_filterable = Column(Boolean, default=True)
    sort_order = Column(Integer, default=0)
    created_at = Column(DateTime, default=utcnow)


class CustomFieldValue(Base):
    __tablename__ = "custom_field_value"

    id = Column(Integer, primary_key=True)
    field_id = Column(Integer, ForeignKey("custom_field.id", ondelete="CASCADE"))
    entity = Column(String, nullable=False)
    entity_id = Column(Integer, nullable=False)

    value_text = Column(Text)
    value_number = Column(Float)
    value_bool = Column(Boolean)
    value_date = Column(DateTime)
    value_json = Column(Text)

    __table_args__ = (
        UniqueConstraint("field_id", "entity", "entity_id", name="uq_cfv"),
        Index("idx_cfv_lookup", "entity", "entity_id"),
        Index("idx_cfv_filter", "field_id", "value_number", "value_bool"),
    )


# --------------------------------------------------------------------------
# Searches / history / ops
# --------------------------------------------------------------------------
class SavedSearch(Base):
    __tablename__ = "saved_search"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    description = Column(Text)
    query_json = Column(Text, nullable=False)
    is_scheduled = Column(Boolean, default=False)
    schedule_cron = Column(String)
    last_run_at = Column(DateTime)
    created_at = Column(DateTime, default=utcnow)


class SearchRun(Base):
    __tablename__ = "search_run"

    id = Column(Integer, primary_key=True)
    saved_search_id = Column(Integer, ForeignKey("saved_search.id",
                                                 ondelete="SET NULL"))
    name = Column(String)
    query_json = Column(Text, nullable=False)
    run_type = Column(String, default="local_query")   # local_query | collection
    started_at = Column(DateTime, default=utcnow)
    finished_at = Column(DateTime)
    results_total = Column(Integer, default=0)
    results_new = Column(Integer, default=0)
    results_duplicate = Column(Integer, default=0)
    results_failed = Column(Integer, default=0)
    sources_used_json = Column(Text)
    status = Column(String, default="running")
    error_summary = Column(Text)


class ScoringRule(Base):
    __tablename__ = "scoring_rule"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    entity = Column(String, nullable=False, default="job")
    is_active = Column(Boolean, default=True)
    weight = Column(Float, default=1.0)
    condition_json = Column(Text, nullable=False)
    points = Column(Float, default=10.0)


class AppSetting(Base):
    __tablename__ = "app_setting"
    key = Column(String, primary_key=True)
    value_json = Column(Text)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)


class CollectionLog(Base):
    __tablename__ = "collection_log"

    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("search_run.id", ondelete="CASCADE"))
    source_key = Column(String)
    level = Column(String, default="info")
    event = Column(String)
    message = Column(Text)
    detail_json = Column(Text)
    created_at = Column(DateTime, default=utcnow)


class ExportLog(Base):
    __tablename__ = "export_log"

    id = Column(Integer, primary_key=True)
    format = Column(String)
    row_count = Column(Integer)
    file_path = Column(Text)
    filters_json = Column(Text)
    created_at = Column(DateTime, default=utcnow)
