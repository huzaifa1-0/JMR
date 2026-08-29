"""Database engine, session handling, FTS5 index and first-run seeding."""
from __future__ import annotations

import csv
import json
import logging
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Iterator

import yaml
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from .config import settings
from .models import Base, AppSetting, ScoringRule, Skill, Source, Company

log = logging.getLogger(__name__)

settings.ensure_dirs()

engine = create_engine(
    settings.db_url,
    connect_args={"check_same_thread": False, "timeout": 30},
    future=True,
)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_conn, _record):
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.close()


SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


def get_db() -> Iterator[Session]:
    """FastAPI dependency."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# --------------------------------------------------------------------------
# Full-text index
# --------------------------------------------------------------------------
FTS_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS job_fts USING fts5(
    title, description_text, company_name,
    tokenize='porter unicode61'
);
"""


def _create_fts(conn) -> None:
    conn.execute(text(FTS_DDL))


def fts_upsert(db: Session, job_id: int, title: str, description: str,
               company: str) -> None:
    db.execute(text("DELETE FROM job_fts WHERE rowid = :i"), {"i": job_id})
    db.execute(
        text("INSERT INTO job_fts(rowid, title, description_text, company_name) "
             "VALUES (:i, :t, :d, :c)"),
        {"i": job_id, "t": title or "", "d": description or "",
         "c": company or ""},
    )


def fts_delete(db: Session, job_id: int) -> None:
    db.execute(text("DELETE FROM job_fts WHERE rowid = :i"), {"i": job_id})


# --------------------------------------------------------------------------
# Seeding
# --------------------------------------------------------------------------
DEFAULT_SOURCES = [
    # key, display, kind, requires_credentials, monthly_quota
    ("remotive",       "Remotive (remote jobs)",     "aggregator", False, None),
    ("adzuna",         "Adzuna",                     "aggregator", True,  1000),
    ("usajobs",        "USAJOBS (US federal)",       "government", True,  None),
    ("greenhouse",     "Greenhouse boards",          "ats",        False, None),
    ("lever",          "Lever boards",               "ats",        False, None),
    ("ashby",          "Ashby boards",               "ats",        False, None),
    ("workable",       "Workable boards",            "ats",        False, None),
]

DEFAULT_SCORING_RULES = [
    ("Salary at or above $80k", "job", 1.0,
     {"field": "salary_annualized_min", "op": "gte", "value": 80000}, 20),
    ("Remote role", "job", 1.0,
     {"field": "work_arrangement", "op": "eq", "value": "remote"}, 15),
    ("Posted within 7 days", "job", 1.0,
     {"field": "posting_age_days", "op": "lte", "value": 7}, 15),
    ("Full-time", "job", 1.0,
     {"field": "employment_type", "op": "eq", "value": "full_time"}, 10),
    ("Urgently hiring", "job", 1.0,
     {"field": "urgently_hiring", "op": "is_true"}, 10),
    ("Company has 5+ open roles", "job", 1.0,
     {"field": "company_jobs_discovered", "op": "gte", "value": 5}, 15),
    ("Full description available", "job", 1.0,
     {"field": "description_length", "op": "gte", "value": 1500}, 10),
    ("Salary disclosed by employer", "job", 1.0,
     {"field": "salary_is_estimated", "op": "is_false"}, 5),
]

DEFAULT_SETTINGS = {
    "max_results_per_run": settings.max_results_per_run,
    "rate_limit_per_minute": settings.rate_limit_per_minute,
    "cache_ttl_minutes": settings.cache_ttl_minutes,
    "default_country": settings.default_country,
    "default_page_size": settings.default_page_size,
    "dupe_title_ratio": settings.dupe_title_ratio,
    "dupe_description_similarity": settings.dupe_description_similarity,
    "export_dir": str(settings.export_dir),
    "adzuna_app_id": settings.adzuna_app_id,
    "adzuna_app_key": settings.adzuna_app_key,
    "usajobs_api_key": settings.usajobs_api_key,
    "usajobs_email": settings.usajobs_email,
}


def _seed_sources(db: Session) -> None:
    existing = {s.key for s in db.query(Source).all()}
    reset = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0,
                                               second=0, microsecond=0) \
        + timedelta(days=32)
    reset = reset.replace(day=1)
    for key, name, kind, creds, quota in DEFAULT_SOURCES:
        if key in existing:
            continue
        db.add(Source(
            key=key, display_name=name, kind=kind,
            requires_credentials=creds, monthly_quota=quota,
            enabled=not creds,          # credentialed sources start disabled
            calls_used_this_period=0, period_reset_at=reset,
            health="not_configured" if creds else "unknown",
            config_json="{}",
        ))


def _seed_skills(db: Session) -> None:
    if db.query(Skill).count():
        return
    path = settings.taxonomy_dir / "skills.yaml"
    if not path.exists():
        return
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    for category, entries in data.items():
        for entry in entries:
            if isinstance(entry, str):
                name, aliases = entry, []
            else:
                name = entry["name"]
                aliases = entry.get("aliases", [])
            db.add(Skill(name=name, category=category,
                         aliases_json=json.dumps(aliases)))


def _seed_scoring(db: Session) -> None:
    if db.query(ScoringRule).count():
        return
    for name, entity, weight, cond, pts in DEFAULT_SCORING_RULES:
        db.add(ScoringRule(name=name, entity=entity, weight=weight,
                           condition_json=json.dumps(cond), points=pts))


def _seed_settings(db: Session) -> None:
    existing = {s.key for s in db.query(AppSetting).all()}
    for k, v in DEFAULT_SETTINGS.items():
        if k not in existing:
            db.add(AppSetting(key=k, value_json=json.dumps(v)))


def _seed_companies(db: Session) -> None:
    if db.query(Company).count():
        return
    path = settings.seed_dir / "ats_companies.csv"
    if not path.exists():
        return
    from .processing.normalize import normalize_company_name
    with path.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            name = (row.get("name") or "").strip()
            if not name:
                continue
            db.add(Company(
                name=name,
                name_normalized=normalize_company_name(name),
                domain=(row.get("domain") or "").strip() or None,
                industry=(row.get("industry") or "").strip() or None,
                ats_platform=(row.get("ats_platform") or "").strip() or None,
                ats_board_token=(row.get("ats_board_token") or "").strip() or None,
            ))


def init_db() -> None:
    """Create tables, FTS index, and seed reference data. Idempotent."""
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        _create_fts(conn)
    with session_scope() as db:
        _seed_sources(db)
        _seed_skills(db)
        _seed_scoring(db)
        _seed_settings(db)
        _seed_companies(db)
    log.info("Database ready at %s", settings.db_path)


# --------------------------------------------------------------------------
# Runtime settings accessor
# --------------------------------------------------------------------------
def get_setting(db: Session, key: str, default=None):
    row = db.get(AppSetting, key)
    if row is None or row.value_json is None:
        return default
    try:
        return json.loads(row.value_json)
    except json.JSONDecodeError:
        return default


def set_setting(db: Session, key: str, value) -> None:
    row = db.get(AppSetting, key)
    if row is None:
        db.add(AppSetting(key=key, value_json=json.dumps(value)))
    else:
        row.value_json = json.dumps(value)
