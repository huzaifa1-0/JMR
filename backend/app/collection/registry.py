"""Source registry and quota accounting."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from ..db import get_setting
from ..models import Source
from .sources.adzuna import AdzunaSource
from .sources.ats import (AshbySource, GreenhouseSource, LeverSource,
                          WorkableSource)
from .sources.remotive import RemotiveSource
from .sources.usajobs import USAJobsSource

log = logging.getLogger(__name__)

SOURCE_CLASSES = {
    "remotive": RemotiveSource,
    "adzuna": AdzunaSource,
    "usajobs": USAJobsSource,
    "greenhouse": GreenhouseSource,
    "lever": LeverSource,
    "ashby": AshbySource,
    "workable": WorkableSource,
}

ATS_KEYS = {"greenhouse", "lever", "ashby", "workable", "recruitee",
            "smartrecruiters"}


def source_config(db: Session, key: str) -> dict:
    """Credentials come from app_setting so they are editable in the UI."""
    row = db.query(Source).filter(Source.key == key).first()
    cfg = {}
    if row and row.config_json:
        try:
            cfg = json.loads(row.config_json)
        except json.JSONDecodeError:
            cfg = {}

    if key == "adzuna":
        cfg.setdefault("app_id", get_setting(db, "adzuna_app_id", ""))
        cfg.setdefault("app_key", get_setting(db, "adzuna_app_key", ""))
    elif key == "usajobs":
        cfg.setdefault("api_key", get_setting(db, "usajobs_api_key", ""))
        cfg.setdefault("email", get_setting(db, "usajobs_email", ""))
    return cfg


def build_sources(db: Session, client, keys: list[str] | None = None) -> list:
    """Instantiate enabled adapters, honouring the enabled flag and quota."""
    rows = db.query(Source).all()
    out = []
    for row in rows:
        if keys is not None and row.key not in keys:
            continue
        if keys is None and not row.enabled:
            continue
        cls = SOURCE_CLASSES.get(row.key)
        if cls is None:
            log.warning("No adapter registered for source '%s'", row.key)
            continue
        if quota_exhausted(row):
            log.info("Skipping '%s': monthly quota spent", row.key)
            continue
        out.append(cls(client, source_config(db, row.key)))
    return out


# --------------------------------------------------------------------------
# Quota
# --------------------------------------------------------------------------
def _next_period_start() -> datetime:
    now = datetime.now(timezone.utc)
    first_next = (now.replace(day=28) + timedelta(days=4)).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0)
    return first_next


def reset_if_due(row: Source) -> None:
    if row.period_reset_at is None:
        row.period_reset_at = _next_period_start()
        return
    reset_at = row.period_reset_at
    if reset_at.tzinfo is None:
        reset_at = reset_at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) >= reset_at:
        log.info("Quota period rolled over for '%s'", row.key)
        row.calls_used_this_period = 0
        row.period_reset_at = _next_period_start()
        if row.health == "quota_exhausted":
            row.health = "unknown"


def quota_exhausted(row: Source) -> bool:
    reset_if_due(row)
    if not row.monthly_quota:
        return False
    return (row.calls_used_this_period or 0) >= row.monthly_quota


def remaining_quota(row: Source) -> int | None:
    reset_if_due(row)
    if not row.monthly_quota:
        return None
    return max(0, row.monthly_quota - (row.calls_used_this_period or 0))


def record_calls(db: Session, key: str, calls: int, *, ok: bool,
                 error: str | None = None) -> None:
    row = db.query(Source).filter(Source.key == key).first()
    if row is None:
        return
    reset_if_due(row)
    row.calls_used_this_period = (row.calls_used_this_period or 0) + calls
    if ok:
        row.last_success_at = datetime.now(timezone.utc)
        row.last_error = None
        row.health = ("quota_exhausted" if quota_exhausted(row) else "ok")
    else:
        row.last_error = (error or "")[:2000]
        row.health = "down"
    db.commit()
