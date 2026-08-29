"""ORM -> API shape."""
from __future__ import annotations

import json

from sqlalchemy.orm import Session

from ..models import CustomField, CustomFieldValue, Job, JobTag, Tag
from ..schemas import JobOut, ResearchOut, SkillOut


def summarize(text: str | None, limit: int = 320) -> str | None:
    if not text:
        return None
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit].rsplit(" ", 1)[0] + "..."


def job_tags(db: Session, job_id: int) -> list[str]:
    rows = (db.query(Tag.name).join(JobTag, JobTag.tag_id == Tag.id)
            .filter(JobTag.job_id == job_id).all())
    return [r[0] for r in rows]


def job_to_out(job: Job, db: Session | None = None,
               with_tags: bool = False) -> JobOut:
    research = None
    if job.research:
        research = ResearchOut(
            status=job.research.status or "new",
            priority=job.research.priority,
            lead_quality=job.research.lead_quality,
            notes=job.research.notes,
            follow_up_at=job.research.follow_up_at,
            tags=job_tags(db, job.id) if (with_tags and db) else [],
        )

    location = job.location_raw or ", ".join(
        p for p in (job.city, job.state) if p) or None

    return JobOut(
        id=job.id,
        title=job.title,
        company=job.company_name_raw or (job.company.name if job.company else None),
        company_id=job.company_id,
        location=location,
        city=job.city,
        state=job.state,
        work_arrangement=job.work_arrangement,
        employment_type=job.employment_type,
        experience_level=job.experience_level,
        education_level=job.education_level,
        years_experience_min=job.years_experience_min,
        salary_min=job.salary_min,
        salary_max=job.salary_max,
        salary_period=job.salary_period,
        salary_annualized_min=job.salary_annualized_min,
        salary_is_estimated=bool(job.salary_is_estimated),
        date_posted=job.date_posted,
        date_collected=job.date_collected,
        job_url=job.job_url,
        source=job.source.key if job.source else None,
        opportunity_score=job.opportunity_score,
        visa_sponsorship_mentioned=job.visa_sponsorship_mentioned,
        relocation_mentioned=job.relocation_mentioned,
        urgently_hiring=job.urgently_hiring,
        description_summary=summarize(job.description_text),
        description_is_truncated=bool(job.description_is_truncated),
        duplicate_count=job.duplicate_count or 0,
        skills=[SkillOut(name=js.skill.name, requirement=js.requirement,
                         mention_count=js.mention_count)
                for js in (job.skills or []) if js.skill][:25],
        research=research,
    )


# --------------------------------------------------------------------------
# Custom field values
# --------------------------------------------------------------------------
def read_value(field: CustomField, row: CustomFieldValue | None):
    if row is None:
        return None
    if field.field_type in ("number", "score"):
        return row.value_number
    if field.field_type == "boolean":
        return row.value_bool
    if field.field_type == "date":
        return row.value_date
    if field.field_type == "multiselect":
        try:
            return json.loads(row.value_json or "[]")
        except json.JSONDecodeError:
            return []
    return row.value_text


def write_value(field: CustomField, row: CustomFieldValue, value) -> None:
    row.value_text = row.value_json = None
    row.value_number = row.value_bool = row.value_date = None
    if value is None or value == "":
        return
    if field.field_type in ("number", "score"):
        row.value_number = float(value)
    elif field.field_type == "boolean":
        row.value_bool = bool(value)
    elif field.field_type == "date":
        row.value_date = value
    elif field.field_type == "multiselect":
        row.value_json = json.dumps(list(value or []))
    else:
        row.value_text = str(value)


def custom_values_for(db: Session, entity: str, entity_id: int) -> dict:
    fields = db.query(CustomField).filter(CustomField.entity == entity).all()
    rows = {r.field_id: r for r in db.query(CustomFieldValue)
            .filter(CustomFieldValue.entity == entity)
            .filter(CustomFieldValue.entity_id == entity_id).all()}
    return {f.key: read_value(f, rows.get(f.id)) for f in fields}
