"""Search, job detail, saved searches and history."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.errors import NotFoundError
from ..db import get_db
from ..models import Job, JobBenefit, SavedSearch, SearchRun, Source
from ..processing import extract as ex
from ..schemas import SavedSearchCreate, SearchQuery, SearchResponse
from ..search.boolean import explain
from ..search.query_builder import build_facets, build_query
from .serializers import custom_values_for, job_tags, job_to_out

router = APIRouter()


# --------------------------------------------------------------------------
@router.post("/search", response_model=SearchResponse)
def search_jobs(query: SearchQuery, with_facets: bool = True,
                db: Session = Depends(get_db)):
    """Search the local corpus. Never touches the network."""
    q = query.model_dump()
    stmt, count_stmt = build_query(db, q)

    total = db.execute(count_stmt).scalar_one()
    offset = (query.page - 1) * query.page_size
    rows = db.execute(stmt.offset(offset).limit(query.page_size)).scalars().all()

    run = SearchRun(
        name=f"Search: {query.job_title or query.keywords or query.boolean or 'all'}"[:120],
        query_json=json.dumps(q, default=str), run_type="local_query",
        started_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
        results_total=total, status="completed")
    db.add(run)
    db.commit()

    facets = build_facets(db, q) if with_facets else None
    pages = max(1, -(-total // query.page_size))

    return SearchResponse(
        total=total, page=query.page, page_size=query.page_size, pages=pages,
        results=[job_to_out(j, db, with_tags=True) for j in rows],
        facets=facets,
        query_echo={k: v for k, v in q.items() if v not in (None, [], "", False)},
    )


@router.get("/search/validate-boolean")
def validate_boolean(expr: str = Query(..., description="Boolean expression")):
    """Live syntax checking for the advanced filter box."""
    return explain(expr)


@router.get("/search/count")
def preview_count(job_title: str | None = None, keywords: str | None = None,
                  boolean: str | None = None, db: Session = Depends(get_db)):
    q = SearchQuery(job_title=job_title, keywords=keywords,
                    boolean=boolean).model_dump()
    _, count_stmt = build_query(db, q)
    return {"count": db.execute(count_stmt).scalar_one()}


# --------------------------------------------------------------------------
@router.get("/jobs/{job_id}")
def job_detail(job_id: int, db: Session = Depends(get_db)):
    job = db.get(Job, job_id)
    if job is None:
        raise NotFoundError(f"Job {job_id} was not found.")

    benefits = [r[0] for r in db.query(JobBenefit.benefit)
                .filter(JobBenefit.job_id == job_id).all()]

    duplicates = db.execute(
        select(Job).where(Job.duplicate_of_id == job_id)
    ).scalars().all()

    base = job_to_out(job, db, with_tags=True).model_dump()
    base.update({
        "description_text": job.description_text,
        "description_html": job.description_html,
        "apply_url": job.apply_url,
        "benefits": benefits,
        "contacts": ex.extract_contacts(job.description_text),
        "custom_fields": custom_values_for(db, "job", job_id),
        "tags": job_tags(db, job_id),
        "source_job_id": job.source_job_id,
        "canonical_hash": job.canonical_hash,
        "is_primary": job.is_primary,
        "duplicate_of_id": job.duplicate_of_id,
        "duplicates": [
            {"id": d.id, "source": d.source.key if d.source else None,
             "job_url": d.job_url, "date_collected": d.date_collected}
            for d in duplicates
        ],
        "company_detail": (
            {"id": job.company.id, "name": job.company.name,
             "industry": job.company.industry, "domain": job.company.domain,
             "ats_platform": job.company.ats_platform,
             "jobs_discovered": job.company.jobs_discovered,
             "hiring_frequency_30d": job.company.hiring_frequency_30d}
            if job.company else None),
    })
    return base


# --------------------------------------------------------------------------
# Saved searches
# --------------------------------------------------------------------------
@router.get("/saved-searches")
def list_saved(db: Session = Depends(get_db)):
    rows = db.query(SavedSearch).order_by(SavedSearch.created_at.desc()).all()
    return [{"id": r.id, "name": r.name, "description": r.description,
             "query": json.loads(r.query_json), "last_run_at": r.last_run_at,
             "created_at": r.created_at} for r in rows]


@router.post("/saved-searches")
def create_saved(payload: SavedSearchCreate, db: Session = Depends(get_db)):
    row = SavedSearch(name=payload.name, description=payload.description,
                      query_json=json.dumps(payload.query.model_dump(),
                                            default=str))
    db.add(row)
    db.commit()
    return {"id": row.id, "name": row.name}


@router.delete("/saved-searches/{search_id}")
def delete_saved(search_id: int, db: Session = Depends(get_db)):
    row = db.get(SavedSearch, search_id)
    if row is None:
        raise NotFoundError("That saved search no longer exists.")
    db.delete(row)
    db.commit()
    return {"deleted": search_id}


@router.post("/saved-searches/{search_id}/run", response_model=SearchResponse)
def run_saved(search_id: int, db: Session = Depends(get_db)):
    row = db.get(SavedSearch, search_id)
    if row is None:
        raise NotFoundError("That saved search no longer exists.")
    row.last_run_at = datetime.now(timezone.utc)
    db.commit()
    return search_jobs(SearchQuery(**json.loads(row.query_json)), True, db)


# --------------------------------------------------------------------------
# History
# --------------------------------------------------------------------------
@router.get("/history")
def history(limit: int = 50, run_type: str | None = None,
            db: Session = Depends(get_db)):
    q = db.query(SearchRun).order_by(SearchRun.started_at.desc())
    if run_type:
        q = q.filter(SearchRun.run_type == run_type)
    rows = q.limit(min(limit, 500)).all()
    return [{
        "id": r.id, "name": r.name, "run_type": r.run_type,
        "started_at": r.started_at, "finished_at": r.finished_at,
        "results_total": r.results_total, "results_new": r.results_new,
        "results_duplicate": r.results_duplicate,
        "results_failed": r.results_failed, "status": r.status,
        "error_summary": r.error_summary,
        "sources": json.loads(r.sources_used_json or "[]"),
        "query": json.loads(r.query_json or "{}"),
    } for r in rows]


@router.get("/stats/overview")
def overview(db: Session = Depends(get_db)):
    total = db.execute(
        select(func.count()).select_from(Job)
        .where(Job.is_primary.is_(True)).where(Job.is_active.is_(True))
    ).scalar_one()
    dupes = db.execute(
        select(func.count()).select_from(Job)
        .where(Job.is_primary.is_(False))).scalar_one()
    companies = db.execute(
        select(func.count(func.distinct(Job.company_id)))
        .where(Job.company_id.isnot(None))).scalar_one()
    with_salary = db.execute(
        select(func.count()).select_from(Job)
        .where(Job.salary_annualized_min.isnot(None))
        .where(Job.is_primary.is_(True))).scalar_one()
    last_run = db.query(SearchRun).filter(
        SearchRun.run_type == "collection").order_by(
        SearchRun.started_at.desc()).first()

    return {
        "jobs_active": total,
        "duplicates_linked": dupes,
        "companies": companies,
        "jobs_with_salary": with_salary,
        "sources": [{"key": s.key, "name": s.display_name, "enabled": s.enabled,
                     "health": s.health,
                     "quota": s.monthly_quota,
                     "used": s.calls_used_this_period}
                    for s in db.query(Source).all()],
        "last_collection": ({
            "id": last_run.id, "at": last_run.started_at,
            "new": last_run.results_new, "status": last_run.status
        } if last_run else None),
    }
