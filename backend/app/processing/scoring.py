"""Configurable opportunity scoring (spec section 18).

Rules are declarative JSON stored in the `scoring_rule` table and edited from
Settings -- the formula is never hard-coded.  A rule looks like:

    {"field": "salary_annualized_min", "op": "gte", "value": 80000}

or, for compound logic:

    {"all": [{"field": "work_arrangement", "op": "eq", "value": "remote"},
             {"field": "posting_age_days",  "op": "lte", "value": 14}]}

Score = 100 * sum(points * weight for matching rules) / sum(points * weight)
"""
from __future__ import annotations

import json
import logging

from sqlalchemy.orm import Session

from ..models import Job, ScoringRule
from .normalize import days_since

log = logging.getLogger(__name__)

OPERATORS = {
    "eq": lambda a, b: a == b,
    "ne": lambda a, b: a != b,
    "gt": lambda a, b: a is not None and a > b,
    "gte": lambda a, b: a is not None and a >= b,
    "lt": lambda a, b: a is not None and a < b,
    "lte": lambda a, b: a is not None and a <= b,
    "contains": lambda a, b: bool(a) and str(b).lower() in str(a).lower(),
    "not_contains": lambda a, b: not (bool(a) and str(b).lower() in str(a).lower()),
    "in": lambda a, b: a in (b or []),
    "not_in": lambda a, b: a not in (b or []),
    "is_true": lambda a, _b=None: a is True,
    "is_false": lambda a, _b=None: a is False,
    "is_null": lambda a, _b=None: a is None,
    "is_not_null": lambda a, _b=None: a is not None,
}


def job_facts(job: Job) -> dict:
    """Flatten a Job (plus a little derived context) into a scoring namespace."""
    company = job.company
    return {
        "title": job.title,
        "company": job.company_name_raw,
        "city": job.city,
        "state": job.state,
        "work_arrangement": job.work_arrangement,
        "employment_type": job.employment_type,
        "experience_level": job.experience_level,
        "education_level": job.education_level,
        "years_experience_min": job.years_experience_min,
        "salary_annualized_min": job.salary_annualized_min,
        "salary_annualized_max": job.salary_annualized_max,
        "salary_is_estimated": bool(job.salary_is_estimated),
        "visa_sponsorship_mentioned": job.visa_sponsorship_mentioned,
        "relocation_mentioned": job.relocation_mentioned,
        "urgently_hiring": bool(job.urgently_hiring),
        "posting_age_days": days_since(job.date_posted),
        "description_length": len(job.description_text or ""),
        "skill_count": len(job.skills or []),
        "skills": [js.skill.name for js in (job.skills or []) if js.skill],
        "company_jobs_discovered": company.jobs_discovered if company else 0,
        "company_industry": company.industry if company else None,
        "company_size_estimate": company.size_estimate if company else None,
        "company_hiring_frequency_30d": (company.hiring_frequency_30d
                                         if company else 0.0),
        "source": job.source.key if job.source else None,
    }


def evaluate_condition(cond: dict, facts: dict) -> bool:
    if not isinstance(cond, dict):
        return False

    if "all" in cond:
        return all(evaluate_condition(c, facts) for c in cond["all"])
    if "any" in cond:
        return any(evaluate_condition(c, facts) for c in cond["any"])
    if "not" in cond:
        return not evaluate_condition(cond["not"], facts)

    field = cond.get("field")
    op = cond.get("op", "eq")
    expected = cond.get("value")
    if field is None or op not in OPERATORS:
        return False

    actual = facts.get(field)

    # list-valued facts (e.g. skills) support contains/in naturally
    if isinstance(actual, list) and op in ("contains", "in"):
        needle = str(expected).lower()
        return any(needle in str(x).lower() for x in actual)

    try:
        return bool(OPERATORS[op](actual, expected))
    except (TypeError, ValueError):
        return False


def score_job(job: Job, rules: list[ScoringRule]) -> float | None:
    """Return 0-100, or None when no active rules exist."""
    active = [r for r in rules if r.is_active and r.entity == "job"]
    if not active:
        return None

    facts = job_facts(job)
    earned = 0.0
    possible = 0.0
    for rule in active:
        weight = rule.weight if rule.weight is not None else 1.0
        pts = (rule.points or 0.0) * weight
        possible += abs(pts)
        try:
            cond = json.loads(rule.condition_json)
        except json.JSONDecodeError:
            log.warning("Scoring rule %s has invalid JSON; skipped", rule.id)
            continue
        if evaluate_condition(cond, facts):
            earned += pts

    if possible <= 0:
        return None
    return round(max(0.0, min(100.0, 100.0 * earned / possible)), 1)


def rescore_jobs(db: Session, job_ids: list[int] | None = None) -> int:
    rules = db.query(ScoringRule).filter(ScoringRule.is_active.is_(True)).all()
    if not rules:
        return 0
    q = db.query(Job)
    if job_ids:
        q = q.filter(Job.id.in_(job_ids))
    count = 0
    for job in q.yield_per(500):
        job.opportunity_score = score_job(job, rules)
        count += 1
    db.commit()
    return count


def score_company(company, db: Session) -> float:
    """Simple, transparent company-level score (spec section 17/18)."""
    jobs = [j for j in company.jobs if j.is_active and j.is_primary]
    if not jobs:
        return 0.0
    volume = min(40.0, len(jobs) * 4.0)
    remote = sum(1 for j in jobs if j.work_arrangement == "remote")
    remote_share = (remote / len(jobs)) * 20.0
    with_salary = sum(1 for j in jobs if j.salary_annualized_min)
    salary_signal = (with_salary / len(jobs)) * 10.0
    avg_salary = [j.salary_annualized_min for j in jobs if j.salary_annualized_min]
    salary_level = 0.0
    if avg_salary:
        mean = sum(avg_salary) / len(avg_salary)
        salary_level = min(20.0, mean / 6000.0)
    frequency = min(10.0, (company.hiring_frequency_30d or 0.0) * 2.0)
    return round(min(100.0, volume + remote_share + salary_signal
                     + salary_level + frequency), 1)
