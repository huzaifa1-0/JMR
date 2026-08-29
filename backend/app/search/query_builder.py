"""Turn a NormalizedQuery into SQLAlchemy.

Every filter in spec sections 3-5 is applied here, against the local corpus.
Searches never touch the network -- that is what makes free-tier quota viable
and searches instant.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import (Select, and_, func, literal_column, or_,
                        select, text)
from sqlalchemy.orm import Session, selectinload

from ..core.errors import InvalidSearchError
from ..models import (Company, CustomField, CustomFieldValue, Job, JobSkill,
                      JobTag, Research, Skill, Source, Tag)
from .boolean import compile_boolean

SORT_FIELDS = {
    "relevance": None,                      # handled specially
    "date_posted": Job.date_posted,
    "date_collected": Job.date_collected,
    "salary": Job.salary_annualized_min,
    "company": Job.company_name_raw,
    "location": Job.city,
    "title": Job.title,
    "remote": Job.work_arrangement,
    "experience": Job.years_experience_min,
    "score": Job.opportunity_score,
}

DATE_PRESETS = {"1": 1, "3": 3, "7": 7, "14": 14, "30": 30}


def _date_floor(q: dict) -> datetime | None:
    preset = q.get("date_posted")
    if preset and str(preset) in DATE_PRESETS:
        return datetime.now(timezone.utc) - timedelta(
            days=DATE_PRESETS[str(preset)])
    if q.get("date_from"):
        try:
            return datetime.fromisoformat(str(q["date_from"]).replace("Z", "+00:00"))
        except ValueError as exc:
            raise InvalidSearchError(
                "The 'from' date could not be read.",
                hint="Use the date picker, or the format YYYY-MM-DD.") from exc
    return None


def _like(col, value: str):
    return col.ilike(f"%{value}%")


def build_query(db: Session, q: dict) -> tuple[Select, Select]:
    """Return (rows_select, count_select)."""
    stmt = (
        select(Job)
        .options(selectinload(Job.company),
                 selectinload(Job.source),
                 selectinload(Job.research),
                 selectinload(Job.skills).selectinload(JobSkill.skill))
    )
    conditions = []

    # -- corpus hygiene -------------------------------------------------
    if not q.get("include_duplicates"):
        conditions.append(Job.is_primary.is_(True))
    if not q.get("include_inactive"):
        conditions.append(Job.is_active.is_(True))

    # -- full text / Boolean --------------------------------------------
    fts_expr = None
    boolean_input = q.get("boolean") or None
    if boolean_input:
        fts_expr = compile_boolean(boolean_input)
    else:
        plain = " ".join(filter(None, [
            q.get("keywords"), q.get("job_title"), q.get("skills_text"),
        ])).strip()
        if plain:
            fts_expr = compile_boolean(plain)

    fts_sub = None
    if fts_expr:
        # FTS5 is a virtual table, so it is joined as an explicit subquery that
        # also carries the bm25 rank out for relevance ordering.
        fts_sub = (
            select(literal_column("rowid").label("job_id"),
                   literal_column("bm25(job_fts)").label("rank"))
            .select_from(text("job_fts"))
            .where(text("job_fts MATCH :fts_expr"))
            .subquery("fts")
        )
        stmt = stmt.join(fts_sub, fts_sub.c.job_id == Job.id)

    # -- basic "what" ----------------------------------------------------
    if q.get("job_title") and not fts_expr:
        conditions.append(_like(Job.title, q["job_title"]))
    if q.get("company"):
        conditions.append(or_(_like(Job.company_name_raw, q["company"]),
                              _like(Company.name, q["company"])))
        stmt = stmt.join(Company, Job.company_id == Company.id, isouter=True)

    # -- basic "where" ---------------------------------------------------
    if q.get("city"):
        conditions.append(_like(Job.city, q["city"]))
    if q.get("state"):
        conditions.append(func.upper(Job.state) == str(q["state"]).upper())
    if q.get("country"):
        conditions.append(func.upper(Job.country) == str(q["country"]).upper())
    if q.get("postal_code"):
        conditions.append(Job.postal_code == str(q["postal_code"]).strip())

    # -- job filters -----------------------------------------------------
    floor = _date_floor(q)
    if floor:
        conditions.append(Job.date_posted >= floor)
    if q.get("date_to"):
        try:
            ceil = datetime.fromisoformat(
                str(q["date_to"]).replace("Z", "+00:00"))
            conditions.append(Job.date_posted <= ceil)
        except ValueError as exc:
            raise InvalidSearchError("The 'to' date could not be read.",
                                     hint="Use the format YYYY-MM-DD.") from exc

    if q.get("employment_types"):
        conditions.append(Job.employment_type.in_(q["employment_types"]))
    if q.get("work_arrangements"):
        conditions.append(Job.work_arrangement.in_(q["work_arrangements"]))
    if q.get("experience_levels"):
        conditions.append(Job.experience_level.in_(q["experience_levels"]))

    if q.get("salary_min") is not None:
        conditions.append(Job.salary_annualized_max.isnot(None)
                          if False else
                          or_(Job.salary_annualized_max >= q["salary_min"],
                              Job.salary_annualized_min >= q["salary_min"]))
    if q.get("salary_max") is not None:
        conditions.append(Job.salary_annualized_min <= q["salary_max"])
    if q.get("has_salary"):
        conditions.append(Job.salary_annualized_min.isnot(None))
    if q.get("employer_stated_salary"):
        conditions.append(Job.salary_is_estimated.is_(False))

    # -- advanced job research ------------------------------------------
    if q.get("title_contains"):
        conditions.append(_like(Job.title, q["title_contains"]))
    if q.get("title_not_contains"):
        conditions.append(~_like(Job.title, q["title_not_contains"]))
    if q.get("description_contains"):
        conditions.append(_like(Job.description_text, q["description_contains"]))
    if q.get("description_not_contains"):
        conditions.append(
            or_(Job.description_text.is_(None),
                ~_like(Job.description_text, q["description_not_contains"])))

    if q.get("education_levels"):
        conditions.append(Job.education_level.in_(q["education_levels"]))
    if q.get("years_experience_min") is not None:
        conditions.append(Job.years_experience_min >= q["years_experience_min"])
    if q.get("years_experience_max") is not None:
        conditions.append(
            or_(Job.years_experience_min.is_(None),
                Job.years_experience_min <= q["years_experience_max"]))

    for flag, column in (("visa_sponsorship", Job.visa_sponsorship_mentioned),
                         ("relocation", Job.relocation_mentioned),
                         ("urgently_hiring", Job.urgently_hiring),
                         ("easy_apply", Job.easy_apply)):
        val = q.get(flag)
        if val is True:
            conditions.append(column.is_(True))
        elif val is False:
            conditions.append(or_(column.is_(False), column.is_(None)))

    if q.get("max_posting_age_days") is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(
            days=int(q["max_posting_age_days"]))
        conditions.append(Job.date_posted >= cutoff)

    if q.get("min_score") is not None:
        conditions.append(Job.opportunity_score >= q["min_score"])

    if q.get("sources"):
        stmt = stmt.join(Source, Job.source_id == Source.id, isouter=True)
        conditions.append(Source.key.in_(q["sources"]))

    # -- required skills (AND semantics) --------------------------------
    required = q.get("required_skills") or []
    if required:
        for name in required:
            sub = (select(JobSkill.job_id)
                   .join(Skill, Skill.id == JobSkill.skill_id)
                   .where(JobSkill.job_id == Job.id)
                   .where(func.lower(Skill.name) == str(name).lower()))
            conditions.append(sub.exists())

    any_skills = q.get("any_skills") or []
    if any_skills:
        sub = (select(JobSkill.job_id)
               .join(Skill, Skill.id == JobSkill.skill_id)
               .where(JobSkill.job_id == Job.id)
               .where(func.lower(Skill.name).in_(
                   [str(s).lower() for s in any_skills])))
        conditions.append(sub.exists())

    excluded_skills = q.get("exclude_skills") or []
    if excluded_skills:
        sub = (select(JobSkill.job_id)
               .join(Skill, Skill.id == JobSkill.skill_id)
               .where(JobSkill.job_id == Job.id)
               .where(func.lower(Skill.name).in_(
                   [str(s).lower() for s in excluded_skills])))
        conditions.append(~sub.exists())

    # -- company research ------------------------------------------------
    company_filters = []
    if q.get("company_industry"):
        company_filters.append(_like(Company.industry, q["company_industry"]))
    if q.get("company_domain"):
        company_filters.append(_like(Company.domain, q["company_domain"]))
    if q.get("company_size_min") is not None:
        company_filters.append(Company.size_estimate >= q["company_size_min"])
    if q.get("company_size_max") is not None:
        company_filters.append(Company.size_estimate <= q["company_size_max"])
    if q.get("company_min_jobs") is not None:
        company_filters.append(Company.jobs_discovered >= q["company_min_jobs"])
    if q.get("company_min_hiring_frequency") is not None:
        company_filters.append(
            Company.hiring_frequency_30d >= q["company_min_hiring_frequency"])
    if q.get("company_ats"):
        company_filters.append(Company.ats_platform.in_(q["company_ats"]))
    if company_filters:
        sub = select(Company.id).where(Company.id == Job.company_id).where(
            and_(*company_filters))
        conditions.append(sub.exists())

    # -- research layer ---------------------------------------------------
    if q.get("research_status"):
        sub = (select(Research.job_id)
               .where(Research.job_id == Job.id)
               .where(Research.status.in_(q["research_status"])))
        conditions.append(sub.exists())
    if q.get("priority"):
        sub = (select(Research.job_id)
               .where(Research.job_id == Job.id)
               .where(Research.priority.in_(q["priority"])))
        conditions.append(sub.exists())
    if q.get("tags"):
        sub = (select(JobTag.job_id)
               .join(Tag, Tag.id == JobTag.tag_id)
               .where(JobTag.job_id == Job.id)
               .where(Tag.name.in_(q["tags"])))
        conditions.append(sub.exists())

    # -- custom fields -----------------------------------------------------
    for cf in (q.get("custom_fields") or []):
        conditions.append(_custom_field_condition(db, cf))

    if conditions:
        stmt = stmt.where(and_(*conditions))

    count_stmt = select(func.count()).select_from(stmt.subquery())

    # -- ordering ----------------------------------------------------------
    sort = q.get("sort") or "relevance"
    direction = (q.get("sort_dir") or "desc").lower()
    if sort == "relevance":
        if fts_sub is not None:
            stmt = stmt.order_by(fts_sub.c.rank.asc(), Job.date_posted.desc())
        else:
            stmt = stmt.order_by(Job.opportunity_score.desc().nullslast(),
                                 Job.date_posted.desc())
    else:
        col = SORT_FIELDS.get(sort)
        if col is None:
            raise InvalidSearchError(
                f"Cannot sort by '{sort}'.",
                hint="Valid options: " + ", ".join(SORT_FIELDS))
        stmt = stmt.order_by(col.desc().nullslast() if direction == "desc"
                             else col.asc().nullsfirst())

    if fts_expr:
        stmt = stmt.params(fts_expr=fts_expr)
        count_stmt = count_stmt.params(fts_expr=fts_expr)

    return stmt, count_stmt


def _custom_field_condition(db: Session, cf: dict):
    key = cf.get("key")
    op = cf.get("op", "eq")
    value = cf.get("value")
    field = db.query(CustomField).filter(CustomField.key == key).first()
    if field is None:
        raise InvalidSearchError(
            f"Unknown custom field '{key}'.",
            hint="It may have been deleted. Remove it from the filter.")

    base = (select(CustomFieldValue.id)
            .where(CustomFieldValue.field_id == field.id)
            .where(CustomFieldValue.entity == "job")
            .where(CustomFieldValue.entity_id == Job.id))

    ftype = field.field_type
    if ftype in ("number", "score"):
        col = CustomFieldValue.value_number
        ops = {"eq": col == value, "gte": col >= value, "lte": col <= value,
               "gt": col > value, "lt": col < value}
        if op not in ops:
            raise InvalidSearchError(f"Operator '{op}' is not valid for a number field.")
        base = base.where(ops[op])
    elif ftype == "boolean":
        base = base.where(CustomFieldValue.value_bool.is_(bool(value)))
    elif ftype == "date":
        col = CustomFieldValue.value_date
        base = base.where(col >= value if op == "gte" else col <= value)
    elif ftype == "multiselect":
        base = base.where(CustomFieldValue.value_json.ilike(f'%"{value}"%'))
    else:
        col = CustomFieldValue.value_text
        if op == "eq":
            base = base.where(func.lower(col) == str(value).lower())
        elif op == "not_contains":
            return ~base.where(col.ilike(f"%{value}%")).exists()
        else:
            base = base.where(col.ilike(f"%{value}%"))

    if op == "is_empty":
        return ~base.exists()
    return base.exists()


# --------------------------------------------------------------------------
# Facets for the results sidebar
# --------------------------------------------------------------------------
def build_facets(db: Session, q: dict, limit: int = 12) -> dict:
    stmt, _ = build_query(db, q)
    sub = stmt.subquery()

    def top(col, n=limit):
        rows = db.execute(
            select(col, func.count().label("n"))
            .select_from(sub)
            .where(col.isnot(None))
            .group_by(col)
            .order_by(func.count().desc())
            .limit(n)
        ).all()
        return [{"value": r[0], "count": r[1]} for r in rows]

    job_ids = select(sub.c.id)
    skill_rows = db.execute(
        select(Skill.name, func.count().label("n"))
        .join(JobSkill, JobSkill.skill_id == Skill.id)
        .where(JobSkill.job_id.in_(job_ids))
        .group_by(Skill.name)
        .order_by(func.count().desc())
        .limit(limit)
    ).all()

    return {
        "company": top(sub.c.company_name_raw),
        "state": top(sub.c.state),
        "city": top(sub.c.city),
        "work_arrangement": top(sub.c.work_arrangement, 5),
        "employment_type": top(sub.c.employment_type, 6),
        "experience_level": top(sub.c.experience_level, 6),
        "skills": [{"value": r[0], "count": r[1]} for r in skill_rows],
    }
