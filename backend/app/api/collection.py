"""Collection endpoints: trigger runs, inspect source health, read logs."""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy.orm import Session

from ..collection.http import HttpClient
from ..collection.orchestrator import (deactivate_stale, refresh_company_metrics,
                                       run_collection)
from ..collection.registry import (SOURCE_CLASSES, build_sources,
                                   remaining_quota, source_config)
from ..core.errors import NotFoundError
from ..db import get_db, get_setting
from ..models import CollectionLog, Company, Source
from ..schemas import CollectionRequest

log = logging.getLogger(__name__)
router = APIRouter()

# In-process status so the top bar can show "collecting..." without polling a DB.
STATUS: dict = {"running": False, "started_at": None, "last_summary": None}


@router.get("/sources")
def list_sources(db: Session = Depends(get_db)):
    out = []
    for row in db.query(Source).order_by(Source.kind, Source.key).all():
        cfg = source_config(db, row.key)
        configured = True
        if row.key == "adzuna":
            configured = bool(cfg.get("app_id") and cfg.get("app_key"))
        elif row.key == "usajobs":
            configured = bool(cfg.get("api_key") and cfg.get("email"))

        # ATS sources are only as useful as the number of boards we know about,
        # so surface that count -- it is what the discovery loop grows.
        ats_boards_known = 0
        if row.kind == "ats":
            ats_boards_known = (db.query(Company)
                                .filter(Company.ats_platform == row.key)
                                .filter(Company.ats_board_token.isnot(None))
                                .count())

        out.append({
            "key": row.key,
            "display_name": row.display_name,
            "kind": row.kind,
            "enabled": bool(row.enabled),
            "requires_credentials": bool(row.requires_credentials),
            "configured": configured,
            "monthly_quota": row.monthly_quota,
            "calls_used": row.calls_used_this_period or 0,
            "remaining": remaining_quota(row),
            "period_reset_at": row.period_reset_at,
            "health": row.health,
            "last_success_at": row.last_success_at,
            "last_error": row.last_error,
            "ats_boards_known": ats_boards_known,
        })
    db.commit()
    return out


@router.post("/sources/{key}/toggle")
def toggle_source(key: str, enabled: bool, db: Session = Depends(get_db)):
    row = db.query(Source).filter(Source.key == key).first()
    if row is None:
        raise NotFoundError(f"No source named '{key}'.")
    row.enabled = enabled
    db.commit()
    return {"key": key, "enabled": enabled}


@router.post("/sources/health-check")
async def health_check(db: Session = Depends(get_db)):
    per_minute = int(get_setting(db, "rate_limit_per_minute", 20))
    results = []
    async with HttpClient(per_minute=per_minute) as client:
        for key in SOURCE_CLASSES:
            cls = SOURCE_CLASSES[key]
            src = cls(client, source_config(db, key))
            try:
                health = await src.health()
            except Exception as exc:                  # noqa: BLE001
                results.append({"key": key, "ok": False, "message": str(exc)})
                continue
            results.append({"key": key, "ok": health.ok,
                            "configured": health.configured,
                            "message": health.message})
            row = db.query(Source).filter(Source.key == key).first()
            if row is not None:
                row.health = ("ok" if health.ok else
                              "not_configured" if not health.configured else "down")
                if not health.ok:
                    row.last_error = health.message[:2000]
    db.commit()
    return results


# --------------------------------------------------------------------------
async def _run_and_record(payload: dict, sources: list[str] | None, name: str):
    STATUS.update({"running": True, "started_at": name, "last_summary": None})
    try:
        summary = await run_collection(payload, sources or None, name)
        STATUS["last_summary"] = summary
        return summary
    except Exception as exc:                          # noqa: BLE001
        log.exception("Collection run failed")
        STATUS["last_summary"] = {"error": str(exc)}
        raise
    finally:
        STATUS["running"] = False


@router.post("/collect")
async def collect(payload: CollectionRequest):
    """Run a collection synchronously and return the full summary."""
    if STATUS["running"]:
        return {"queued": False,
                "message": "A collection run is already in progress.",
                "hint": "Wait for it to finish, then try again."}
    data = payload.model_dump()
    sources = data.pop("sources", []) or None
    name = data.pop("name", None) or "Collection run"
    return await _run_and_record(data, sources, name)


@router.post("/collect/background")
async def collect_background(payload: CollectionRequest,
                             background: BackgroundTasks):
    """Fire-and-forget variant for large runs; poll /collect/status."""
    if STATUS["running"]:
        return {"queued": False,
                "message": "A collection run is already in progress."}
    data = payload.model_dump()
    sources = data.pop("sources", []) or None
    name = data.pop("name", None) or "Collection run"

    def _kick():
        asyncio.run(_run_and_record(data, sources, name))

    background.add_task(_kick)
    return {"queued": True, "message": "Collection started in the background."}


@router.get("/collect/status")
def collect_status():
    return STATUS


@router.get("/collect/logs")
def collection_logs(run_id: int | None = None, limit: int = 200,
                    level: str | None = None, db: Session = Depends(get_db)):
    q = db.query(CollectionLog).order_by(CollectionLog.created_at.desc())
    if run_id:
        q = q.filter(CollectionLog.run_id == run_id)
    if level:
        q = q.filter(CollectionLog.level == level)
    return [{
        "id": r.id, "run_id": r.run_id, "source": r.source_key,
        "level": r.level, "event": r.event, "message": r.message,
        "detail": json.loads(r.detail_json) if r.detail_json else None,
        "created_at": r.created_at,
    } for r in q.limit(min(limit, 1000)).all()]


@router.post("/maintenance/refresh-companies")
def refresh_companies(db: Session = Depends(get_db)):
    return {"companies_updated": refresh_company_metrics(db)}


@router.post("/maintenance/deactivate-stale")
def stale(days: int = 45, db: Session = Depends(get_db)):
    return {"deactivated": deactivate_stale(db, days), "older_than_days": days}
