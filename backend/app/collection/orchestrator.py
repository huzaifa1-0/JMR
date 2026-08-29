"""Collection orchestration: fetch -> normalise -> dedupe -> extract -> score -> store.

A run is deliberately budgeted.  It never spends more calls than Settings
allows, never exceeds a source's monthly quota, and writes a full audit trail to
`collection_log` so any surprising result can be traced back to the response
that produced it.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import fts_upsert, get_setting, session_scope
from ..models import (CollectionLog, Company, Job, JobBenefit, JobSkill,
                      ScoringRule, SearchRun, Source)
from ..processing import dedupe as dd
from ..processing import extract as ex
from ..processing import normalize as nz
from ..processing.scoring import score_job
from .ats_discovery import discover_from_postings
from .base import Budget, CollectionQuery, RawPosting
from .http import HttpClient
from .registry import ATS_KEYS, build_sources, record_calls, remaining_quota

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Logging helper
# --------------------------------------------------------------------------
def _log(db: Session, run_id: int | None, source: str, event: str,
         message: str, level: str = "info", **detail) -> None:
    db.add(CollectionLog(run_id=run_id, source_key=source, level=level,
                         event=event, message=message,
                         detail_json=json.dumps(detail, default=str)
                         if detail else None))
    getattr(log, level if level in ("info", "warning", "error") else "info")(
        "[%s] %s: %s", source, event, message)


# --------------------------------------------------------------------------
# Company resolution
# --------------------------------------------------------------------------
def resolve_company(db: Session, name: str | None, *, domain: str | None = None,
                    website: str | None = None,
                    ats: tuple[str, str] | None = None) -> Company | None:
    if not name:
        return None
    normalized = nz.normalize_company_name(name)
    if not normalized:
        return None

    company = db.execute(
        select(Company).where(Company.name_normalized == normalized).limit(1)
    ).scalar_one_or_none()

    if company is None:
        company = Company(name=name.strip(), name_normalized=normalized,
                          domain=domain, website=website,
                          first_seen_at=datetime.now(timezone.utc))
        db.add(company)
        db.flush()

    company.last_seen_at = datetime.now(timezone.utc)
    if domain and not company.domain:
        company.domain = domain
    if website and not company.website:
        company.website = website
    if ats and not company.ats_board_token:
        company.ats_platform, company.ats_board_token = ats
        log.info("Registered %s board '%s' for %s", ats[0], ats[1], company.name)
    return company


# --------------------------------------------------------------------------
# Posting -> Job
# --------------------------------------------------------------------------
def build_job_fields(posting: RawPosting) -> dict:
    """Everything derivable from one posting, before it touches the DB."""
    description_text = posting.description_text or nz.html_to_text(
        posting.description_html)

    loc = {"city": posting.city, "state": posting.state,
           "country": posting.country or "US",
           "postal_code": posting.postal_code}
    if not (posting.city and posting.state) and posting.location_raw:
        parsed = nz.parse_location(posting.location_raw)
        loc["city"] = posting.city or parsed["city"]
        loc["state"] = posting.state or parsed["state"]
        loc["postal_code"] = posting.postal_code or parsed["postal_code"]

    # A source-level remote flag is a strong hint but not the last word: boards
    # that only list remote-friendly roles still carry hybrid postings, and the
    # description is the more specific evidence. Text saying "hybrid" wins.
    detected = nz.detect_work_arrangement(
        posting.title, posting.location_raw, description_text[:4000])
    if detected == "hybrid":
        arrangement = "hybrid"
    elif posting.is_remote is True:
        arrangement = "remote"
    else:
        arrangement = detected

    employment = nz.detect_employment_type(
        posting.employment_type, posting.title, description_text[:2000])

    experience = ex.extract_experience(description_text)
    level = nz.detect_experience_level(posting.title, description_text,
                                       experience["years_experience_min"])

    if posting.salary_min or posting.salary_max:
        salary = nz.normalize_salary(posting.salary_min, posting.salary_max,
                                     posting.salary_period,
                                     posting.salary_currency)
        salary["salary_is_estimated"] = posting.salary_is_estimated
    else:
        parsed_salary = nz.parse_salary_from_text(description_text)
        if parsed_salary:
            salary = nz.normalize_salary(parsed_salary["salary_min"],
                                         parsed_salary["salary_max"],
                                         parsed_salary["salary_period"],
                                         "USD")
            salary["salary_is_estimated"] = False
        else:
            salary = {"salary_min": None, "salary_max": None,
                      "salary_period": None, "salary_currency": "USD",
                      "salary_annualized_min": None,
                      "salary_annualized_max": None,
                      "salary_is_estimated": False}

    flags = ex.extract_flags(description_text, posting.title)
    flags.pop("is_recruitment_agency", None)

    return {
        "title": posting.title,
        "title_normalized": nz.normalize_title(posting.title),
        "description_html": posting.description_html,
        "description_text": description_text or None,
        "description_is_truncated": posting.description_is_truncated,
        "location_raw": posting.location_raw,
        "latitude": posting.latitude,
        "longitude": posting.longitude,
        "work_arrangement": arrangement,
        "employment_type": employment,
        "experience_level": level,
        "education_level": ex.extract_education(description_text),
        "date_posted": nz.parse_date(posting.date_posted),
        "job_url": posting.job_url,
        "apply_url": posting.apply_url,
        "simhash": dd.simhash(description_text),
        **loc, **experience, **salary, **flags,
    }


def _apply_skills_and_benefits(db: Session, job: Job, text: str) -> None:
    db.query(JobSkill).filter(JobSkill.job_id == job.id).delete()
    for hit in ex.extract_skills(db, text):
        db.add(JobSkill(job_id=job.id, skill_id=hit["skill_id"],
                        requirement=hit["requirement"],
                        mention_count=hit["mention_count"]))
    db.query(JobBenefit).filter(JobBenefit.job_id == job.id).delete()
    for benefit in ex.extract_benefits(text):
        db.add(JobBenefit(job_id=job.id, benefit=benefit))


# --------------------------------------------------------------------------
# Ingest
# --------------------------------------------------------------------------
def ingest_posting(db: Session, posting: RawPosting, source_row: Source,
                   thresholds: tuple[float, float],
                   rules: list[ScoringRule]) -> str:
    """Returns 'new' | 'updated' | 'duplicate' | 'failed'."""
    from .ats_discovery import detect_ats

    fields = build_job_fields(posting)

    ats = detect_ats(posting.apply_url) or detect_ats(posting.job_url)
    company = resolve_company(db, posting.company_name,
                              domain=posting.company_domain,
                              website=posting.company_website, ats=ats)

    existing = db.execute(
        select(Job)
        .where(Job.source_id == source_row.id)
        .where(Job.source_job_id == posting.source_job_id)
        .limit(1)
    ).scalar_one_or_none()

    now = datetime.now(timezone.utc)

    if existing is not None:
        for key, value in fields.items():
            # never overwrite a full description with a truncated one
            if key == "description_text" and existing.description_text \
                    and not existing.description_is_truncated \
                    and posting.description_is_truncated:
                continue
            setattr(existing, key, value)
        existing.last_seen_at = now
        existing.is_active = True
        existing.raw_json = json.dumps(posting.raw, default=str)
        if company:
            existing.company_id = company.id
        db.flush()
        _apply_skills_and_benefits(db, existing, fields["description_text"] or "")
        db.flush()
        existing.opportunity_score = score_job(existing, rules)
        fts_upsert(db, existing.id, existing.title,
                   existing.description_text or "", existing.company_name_raw or "")
        return "updated"

    verdict = dd.find_duplicate(
        db, title=posting.title, company=posting.company_name,
        city=fields["city"], state=fields["state"],
        description=fields["description_text"], job_url=posting.job_url,
        source_key=source_row.key,
        title_threshold=thresholds[0], desc_threshold=thresholds[1])

    job = Job(
        source_id=source_row.id,
        source_job_id=posting.source_job_id,
        company_id=company.id if company else None,
        company_name_raw=posting.company_name,
        date_collected=now,
        last_seen_at=now,
        is_active=True,
        canonical_hash=dd.canonical_hash(posting.title, posting.company_name,
                                         fields["city"], fields["state"]),
        raw_json=json.dumps(posting.raw, default=str),
        **fields,
    )

    outcome = "new"
    if verdict.is_duplicate and verdict.primary_id:
        primary = db.get(Job, verdict.primary_id)
        if primary is not None:
            primary_source = primary.source.key if primary.source else None
            if dd.should_promote(source_row.key, primary_source):
                # incoming record is richer -- it becomes primary
                primary.is_primary = False
                primary.duplicate_of_id = None
                job.is_primary = True
                db.add(job)
                db.flush()
                primary.duplicate_of_id = job.id
                job.duplicate_count = (primary.duplicate_count or 0) + 1
            else:
                job.is_primary = False
                job.duplicate_of_id = primary.id
                primary.duplicate_count = (primary.duplicate_count or 0) + 1
                db.add(job)
                db.flush()
            outcome = "duplicate"
        else:
            db.add(job)
            db.flush()
    else:
        db.add(job)
        db.flush()

    _apply_skills_and_benefits(db, job, fields["description_text"] or "")
    db.flush()
    job.opportunity_score = score_job(job, rules)

    if job.is_primary:
        fts_upsert(db, job.id, job.title, job.description_text or "",
                   job.company_name_raw or "")

    if company:
        company.jobs_discovered = db.execute(
            select(func.count()).select_from(Job)
            .where(Job.company_id == company.id)
            .where(Job.is_primary.is_(True))
        ).scalar_one()

    return outcome


# --------------------------------------------------------------------------
# Run
# --------------------------------------------------------------------------
def _ats_tokens_for(db: Session, platform: str, limit: int,
                    company_filter: str | None = None) -> list[tuple[str, str]]:
    q = (select(Company.ats_board_token, Company.name)
         .where(Company.ats_platform == platform)
         .where(Company.ats_board_token.isnot(None)))
    if company_filter:
        q = q.where(Company.name.ilike(f"%{company_filter}%"))
    q = q.order_by(Company.last_seen_at.asc().nullsfirst()).limit(limit)
    return [(row[0], row[1]) for row in db.execute(q).all()]


async def _gather(sources, query: CollectionQuery, budgets: dict) -> dict:
    async def run_one(src):
        try:
            postings = await src.search(query, budgets[src.key])
            return src.key, list(postings), None
        except Exception as exc:                      # noqa: BLE001
            log.warning("[%s] collection failed: %s", src.key, exc)
            return src.key, [], exc

    results = await asyncio.gather(*(run_one(s) for s in sources))
    return {key: (postings, err) for key, postings, err in results}


async def run_collection(query_dict: dict, source_keys: list[str] | None = None,
                         run_name: str | None = None) -> dict:
    """Execute one collection run. Returns a summary dict."""
    with session_scope() as db:
        max_results = int(get_setting(db, "max_results_per_run", 500))
        per_minute = int(get_setting(db, "rate_limit_per_minute", 20))
        ttl = int(get_setting(db, "cache_ttl_minutes", 60))
        thresholds = (float(get_setting(db, "dupe_title_ratio", 0.92)),
                      float(get_setting(db, "dupe_description_similarity", 0.85)))

        run = SearchRun(name=run_name or "Collection run",
                        query_json=json.dumps(query_dict, default=str),
                        run_type="collection", status="running",
                        started_at=datetime.now(timezone.utc))
        db.add(run)
        db.commit()
        run_id = run.id

        source_rows = {s.key: s for s in db.query(Source).all()}
        query = CollectionQuery(
            keywords=query_dict.get("keywords"),
            job_title=query_dict.get("job_title"),
            company=query_dict.get("company"),
            location=query_dict.get("location"),
            city=query_dict.get("city"),
            state=query_dict.get("state"),
            country=(query_dict.get("country") or "us").lower(),
            postal_code=query_dict.get("postal_code"),
            remote_only=bool(query_dict.get("remote_only")),
            max_days_old=query_dict.get("max_days_old"),
            salary_min=query_dict.get("salary_min"),
            employment_type=query_dict.get("employment_type"),
        )

    summary = {"run_id": run_id, "new": 0, "updated": 0, "duplicate": 0,
               "failed": 0, "total": 0, "sources": {}, "errors": [],
               "discovered_boards": 0}

    async with HttpClient(per_minute=per_minute, ttl_minutes=ttl) as client:
        # ---- pass 1: aggregators & government -------------------------
        with session_scope() as db:
            sources = build_sources(db, client, source_keys)
            budgets = {}
            for src in sources:
                row = source_rows.get(src.key)
                quota_left = remaining_quota(row) if row else None
                max_calls = min(25, quota_left) if quota_left is not None else 25
                budgets[src.key] = Budget(max_calls=max_calls,
                                          max_results=max_results)
            non_ats = [s for s in sources if s.key not in ATS_KEYS]
            ats = [s for s in sources if s.key in ATS_KEYS]

        harvest: dict[str, tuple[list[RawPosting], Exception | None]] = {}
        if non_ats:
            harvest |= await _gather(non_ats, query, budgets)

        # ---- ATS discovery from what we just found --------------------
        discovered = {}
        for key, (postings, _err) in harvest.items():
            discovered |= discover_from_postings(postings)

        if discovered:
            with session_scope() as db:
                for name, (platform, token) in discovered.items():
                    resolve_company(db, name, ats=(platform, token))
                summary["discovered_boards"] = len(discovered)

        # ---- pass 2: ATS boards ---------------------------------------
        if ats:
            with session_scope() as db:
                for src in ats:
                    tokens = _ats_tokens_for(db, src.key, limit=25,
                                             company_filter=query.company)
                    if not tokens:
                        continue
                    ats_query = CollectionQuery(
                        keywords=query.keywords, job_title=query.job_title,
                        company_tokens=tokens, country=query.country)
                    harvest[src.key] = (await src.search(
                        ats_query, budgets[src.key]), None)

        # ---- ingest ---------------------------------------------------
        with session_scope() as db:
            rules = db.query(ScoringRule).filter(
                ScoringRule.is_active.is_(True)).all()

            for key, (postings, err) in harvest.items():
                src_row = db.query(Source).filter(Source.key == key).first()
                if src_row is None:
                    continue
                stats = {"new": 0, "updated": 0, "duplicate": 0, "failed": 0}

                if err is not None:
                    _log(db, run_id, key, "source_error", str(err), "warning")
                    summary["errors"].append({"source": key, "message": str(err)})

                for posting in postings:
                    try:
                        outcome = ingest_posting(db, posting, src_row,
                                                 thresholds, rules)
                        stats[outcome] = stats.get(outcome, 0) + 1
                    except Exception as exc:          # noqa: BLE001
                        db.rollback()
                        stats["failed"] += 1
                        _log(db, run_id, key, "ingest_failed",
                             f"{posting.source_job_id}: {exc}", "warning")

                db.commit()
                budget = budgets.get(key)
                record_calls(db, key, budget.calls_used if budget else 0,
                             ok=err is None, error=str(err) if err else None)

                summary["sources"][key] = stats | {
                    "calls": budget.calls_used if budget else 0,
                    "fetched": len(postings),
                }
                for k in ("new", "updated", "duplicate", "failed"):
                    summary[k] += stats[k]
                _log(db, run_id, key, "source_complete",
                     f"{len(postings)} fetched, {stats['new']} new", "info",
                     **stats)

            summary["total"] = summary["new"] + summary["updated"] \
                + summary["duplicate"]

            run = db.get(SearchRun, run_id)
            run.finished_at = datetime.now(timezone.utc)
            run.results_total = summary["total"]
            run.results_new = summary["new"]
            run.results_duplicate = summary["duplicate"]
            run.results_failed = summary["failed"]
            run.sources_used_json = json.dumps(list(harvest.keys()))
            run.status = "partial" if summary["errors"] else "completed"
            if summary["errors"]:
                run.error_summary = "; ".join(
                    e["message"][:200] for e in summary["errors"])[:2000]
            db.commit()

    return summary


# --------------------------------------------------------------------------
# Maintenance
# --------------------------------------------------------------------------
def refresh_company_metrics(db: Session) -> int:
    """Recompute jobs_discovered and 30-day hiring frequency."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    companies = db.query(Company).all()
    for company in companies:
        total = db.execute(
            select(func.count()).select_from(Job)
            .where(Job.company_id == company.id)
            .where(Job.is_primary.is_(True))
        ).scalar_one()
        recent = db.execute(
            select(func.count()).select_from(Job)
            .where(Job.company_id == company.id)
            .where(Job.is_primary.is_(True))
            .where(Job.date_posted >= cutoff)
        ).scalar_one()
        company.jobs_discovered = total
        company.hiring_frequency_30d = round(recent / 4.3, 2)
    db.commit()
    return len(companies)


def deactivate_stale(db: Session, days: int = 45) -> int:
    """Postings not seen in a while age out. Never deleted -- history matters."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (db.query(Job)
            .filter(Job.is_active.is_(True))
            .filter(Job.last_seen_at < cutoff).all())
    for job in rows:
        job.is_active = False
    db.commit()
    return len(rows)
