"""Company research (section 17) and analytics (sections 15-16)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from ..core.errors import NotFoundError
from ..db import get_db
from ..models import Company, Job, JobSkill, Skill, Source
from ..processing.scoring import score_company
from ..schemas import SearchQuery
from ..search.query_builder import build_query
from .serializers import custom_values_for, job_to_out

router = APIRouter()

SALARY_BANDS = [
    (0, 40000, "Under $40k"),
    (40000, 60000, "$40k-$60k"),
    (60000, 80000, "$60k-$80k"),
    (80000, 100000, "$80k-$100k"),
    (100000, 130000, "$100k-$130k"),
    (130000, 160000, "$130k-$160k"),
    (160000, 10**9, "$160k+"),
]


def _scope(db: Session, query: SearchQuery | None):
    """Analytics respect the active filters, not just the whole database."""
    q = (query or SearchQuery()).model_dump()
    stmt, _ = build_query(db, q)
    return stmt.subquery()


# --------------------------------------------------------------------------
# Companies
# --------------------------------------------------------------------------
@router.get("/companies")
def list_companies(q: str | None = None, industry: str | None = None,
                   ats: str | None = None, min_jobs: int = 0,
                   sort: str = "jobs", limit: int = 100, offset: int = 0,
                   db: Session = Depends(get_db)):
    query = db.query(Company)
    if q:
        query = query.filter(Company.name.ilike(f"%{q}%"))
    if industry:
        query = query.filter(Company.industry.ilike(f"%{industry}%"))
    if ats:
        query = query.filter(Company.ats_platform == ats)
    if min_jobs:
        query = query.filter(Company.jobs_discovered >= min_jobs)

    order = {
        "jobs": Company.jobs_discovered.desc(),
        "name": Company.name.asc(),
        "frequency": Company.hiring_frequency_30d.desc(),
        "score": Company.opportunity_score.desc(),
        "recent": Company.last_seen_at.desc(),
    }.get(sort, Company.jobs_discovered.desc())

    total = query.count()
    rows = query.order_by(order).offset(offset).limit(min(limit, 500)).all()
    return {
        "total": total,
        "results": [{
            "id": c.id, "name": c.name, "domain": c.domain,
            "industry": c.industry, "size_bucket": c.size_bucket,
            "hq_location": c.hq_location, "ats_platform": c.ats_platform,
            "ats_board_token": c.ats_board_token,
            "jobs_discovered": c.jobs_discovered or 0,
            "hiring_frequency_30d": c.hiring_frequency_30d or 0.0,
            "opportunity_score": c.opportunity_score,
            "last_seen_at": c.last_seen_at,
        } for c in rows],
    }


@router.get("/companies/{company_id}")
def company_detail(company_id: int, db: Session = Depends(get_db)):
    company = db.get(Company, company_id)
    if company is None:
        raise NotFoundError(f"Company {company_id} was not found.")

    jobs = [j for j in company.jobs if j.is_primary]
    active = [j for j in jobs if j.is_active]

    def tally(attr):
        out: dict = {}
        for j in active:
            key = getattr(j, attr) or "unknown"
            out[key] = out.get(key, 0) + 1
        return sorted([{"value": k, "count": v} for k, v in out.items()],
                      key=lambda x: -x["count"])

    skill_rows = db.execute(
        select(Skill.name, func.count().label("n"))
        .join(JobSkill, JobSkill.skill_id == Skill.id)
        .join(Job, Job.id == JobSkill.job_id)
        .where(Job.company_id == company_id)
        .where(Job.is_primary.is_(True))
        .group_by(Skill.name).order_by(func.count().desc()).limit(20)
    ).all()

    salaries = [j.salary_annualized_min for j in active if j.salary_annualized_min]

    # posting cadence, last 6 months
    cadence_rows = db.execute(
        select(func.strftime("%Y-%m", Job.date_posted), func.count())
        .where(Job.company_id == company_id)
        .where(Job.is_primary.is_(True))
        .where(Job.date_posted.isnot(None))
        .group_by(func.strftime("%Y-%m", Job.date_posted))
        .order_by(func.strftime("%Y-%m", Job.date_posted).desc()).limit(6)
    ).all()

    company.opportunity_score = score_company(company, db)
    db.commit()

    return {
        "id": company.id, "name": company.name, "domain": company.domain,
        "website": company.website, "industry": company.industry,
        "size_bucket": company.size_bucket, "size_estimate": company.size_estimate,
        "hq_location": company.hq_location,
        "ats_platform": company.ats_platform,
        "ats_board_token": company.ats_board_token,
        "first_seen_at": company.first_seen_at,
        "last_seen_at": company.last_seen_at,
        "hiring_frequency_30d": company.hiring_frequency_30d,
        "opportunity_score": company.opportunity_score,
        "research_notes": company.research_notes,
        "jobs_total": len(jobs),
        "jobs_active": len(active),
        "jobs_remote": sum(1 for j in active if j.work_arrangement == "remote"),
        "by_location": tally("city"),
        "by_employment_type": tally("employment_type"),
        "by_experience_level": tally("experience_level"),
        "by_work_arrangement": tally("work_arrangement"),
        "top_skills": [{"value": r[0], "count": r[1]} for r in skill_rows],
        "salary": {
            "count": len(salaries),
            "min": min(salaries) if salaries else None,
            "max": max(salaries) if salaries else None,
            "avg": round(sum(salaries) / len(salaries)) if salaries else None,
        },
        "posting_cadence": [{"month": r[0], "count": r[1]}
                            for r in reversed(cadence_rows)],
        "custom_fields": custom_values_for(db, "company", company_id),
        "jobs": [job_to_out(j, db).model_dump() for j in
                 sorted(active, key=lambda x: x.date_posted or datetime.min.replace(
                     tzinfo=timezone.utc), reverse=True)[:100]],
    }


@router.put("/companies/{company_id}/notes")
def update_company_notes(company_id: int, notes: str = Query(...),
                         db: Session = Depends(get_db)):
    company = db.get(Company, company_id)
    if company is None:
        raise NotFoundError(f"Company {company_id} was not found.")
    company.research_notes = notes
    db.commit()
    return {"id": company_id, "research_notes": notes}


# --------------------------------------------------------------------------
# Analytics
# --------------------------------------------------------------------------
@router.post("/analytics/overview")
def analytics_overview(query: SearchQuery | None = None,
                       db: Session = Depends(get_db)):
    sub = _scope(db, query)

    def group(col, limit=15):
        rows = db.execute(
            select(col, func.count()).select_from(sub)
            .where(col.isnot(None)).group_by(col)
            .order_by(func.count().desc()).limit(limit)
        ).all()
        return [{"label": r[0], "count": r[1]} for r in rows]

    total = db.execute(select(func.count()).select_from(sub)).scalar_one()

    # salary bands
    band_case = case(
        *[(sub.c.salary_annualized_min < hi, label)
          for _lo, hi, label in SALARY_BANDS],
        else_="Unknown")
    band_rows = db.execute(
        select(band_case, func.count()).select_from(sub)
        .where(sub.c.salary_annualized_min.isnot(None))
        .group_by(band_case)
    ).all()
    band_order = {label: i for i, (_l, _h, label) in enumerate(SALARY_BANDS)}
    bands = sorted([{"label": r[0], "count": r[1]} for r in band_rows],
                   key=lambda x: band_order.get(x["label"], 99))

    # postings over time
    timeline = db.execute(
        select(func.date(sub.c.date_posted), func.count()).select_from(sub)
        .where(sub.c.date_posted.isnot(None))
        .group_by(func.date(sub.c.date_posted))
        .order_by(func.date(sub.c.date_posted).desc()).limit(60)
    ).all()

    # skills
    job_ids = select(sub.c.id)
    skill_rows = db.execute(
        select(Skill.name, func.count(), Skill.category)
        .join(JobSkill, JobSkill.skill_id == Skill.id)
        .where(JobSkill.job_id.in_(job_ids))
        .group_by(Skill.name, Skill.category)
        .order_by(func.count().desc()).limit(30)
    ).all()

    salary_stats = db.execute(
        select(func.avg(sub.c.salary_annualized_min),
               func.min(sub.c.salary_annualized_min),
               func.max(sub.c.salary_annualized_max))
        .select_from(sub).where(sub.c.salary_annualized_min.isnot(None))
    ).one()

    remote_rows = db.execute(
        select(sub.c.work_arrangement, func.count()).select_from(sub)
        .group_by(sub.c.work_arrangement)
    ).all()

    return {
        "total_jobs": total,
        "by_company": group(sub.c.company_name_raw),
        "by_state": group(sub.c.state),
        "by_city": group(sub.c.city),
        "by_title": group(sub.c.title_normalized),
        "by_employment_type": group(sub.c.employment_type, 8),
        "by_experience_level": group(sub.c.experience_level, 8),
        "by_education": group(sub.c.education_level, 8),
        "by_work_arrangement": [{"label": r[0] or "unknown", "count": r[1]}
                                for r in remote_rows],
        "by_salary_band": bands,
        "timeline": [{"date": r[0], "count": r[1]} for r in reversed(timeline)],
        "top_skills": [{"label": r[0], "count": r[1], "category": r[2]}
                       for r in skill_rows],
        "salary_summary": {
            "avg_annualized": round(salary_stats[0]) if salary_stats[0] else None,
            "min": salary_stats[1], "max": salary_stats[2],
        },
        "coverage_note": (
            "Counts reflect this local corpus, not the whole US job market. "
            "Aggregator and ATS sources under-represent small local employers "
            "and staffing agencies. Treat rankings and trends as reliable; "
            "treat absolute totals as a sample."
        ),
    }


@router.post("/analytics/skills")
def skills_analysis(query: SearchQuery | None = None, limit: int = 100,
                    category: str | None = None,
                    db: Session = Depends(get_db)):
    """Spec section 16: which skills recur across the corpus."""
    sub = _scope(db, query)
    total = db.execute(select(func.count()).select_from(sub)).scalar_one()
    job_ids = select(sub.c.id)

    stmt = (select(Skill.name, Skill.category, func.count().label("n"),
                   func.sum(case((JobSkill.requirement == "required", 1),
                                 else_=0)),
                   func.sum(case((JobSkill.requirement == "preferred", 1),
                                 else_=0)))
            .join(JobSkill, JobSkill.skill_id == Skill.id)
            .where(JobSkill.job_id.in_(job_ids)))
    if category:
        stmt = stmt.where(Skill.category == category)
    stmt = stmt.group_by(Skill.name, Skill.category) \
               .order_by(func.count().desc()).limit(min(limit, 500))

    rows = db.execute(stmt).all()
    return {
        "total_jobs_in_scope": total,
        "skills": [{
            "skill": r[0], "category": r[1], "jobs": r[2],
            "required_in": r[3] or 0, "preferred_in": r[4] or 0,
            "share": round(100.0 * r[2] / total, 1) if total else 0.0,
        } for r in rows],
    }


@router.get("/analytics/hiring-companies")
def top_hiring(days: int = 30, limit: int = 25,
               db: Session = Depends(get_db)):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    rows = db.execute(
        select(Company.id, Company.name, Company.industry,
               Company.ats_platform, func.count(Job.id))
        .join(Job, Job.company_id == Company.id)
        .where(Job.is_primary.is_(True))
        .where(Job.date_posted >= cutoff)
        .group_by(Company.id)
        .order_by(func.count(Job.id).desc()).limit(min(limit, 200))
    ).all()
    return [{"id": r[0], "name": r[1], "industry": r[2], "ats_platform": r[3],
             "jobs_posted": r[4], "window_days": days} for r in rows]


@router.get("/skills")
def list_skills(q: str | None = None, category: str | None = None,
                limit: int = 200, db: Session = Depends(get_db)):
    query = db.query(Skill)
    if q:
        query = query.filter(Skill.name.ilike(f"%{q}%"))
    if category:
        query = query.filter(Skill.category == category)
    rows = query.order_by(Skill.name).limit(min(limit, 1000)).all()
    return [{"id": s.id, "name": s.name, "category": s.category} for s in rows]
