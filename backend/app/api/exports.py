"""Export (section 14) and settings (section 23)."""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from ..config import settings as cfg
from ..core.errors import ExportError, NotFoundError
from ..db import get_db, get_setting, set_setting
from ..models import (CustomField, CustomFieldValue, ExportLog, JobBenefit,
                      Skill, Source)
from ..processing.extract import invalidate_matcher
from ..schemas import ExportRequest, SettingUpdate
from ..search.query_builder import build_query
from .serializers import job_tags, read_value

log = logging.getLogger(__name__)
router = APIRouter()

BASE_COLUMNS = [
    "id", "title", "company", "location", "city", "state", "country",
    "work_arrangement", "employment_type", "experience_level",
    "education_level", "years_experience_min", "salary_min", "salary_max",
    "salary_period", "salary_annualized_min", "salary_annualized_max",
    "salary_is_estimated", "visa_sponsorship_mentioned",
    "relocation_mentioned", "urgently_hiring", "date_posted",
    "date_collected", "source", "job_url", "opportunity_score",
    "skills", "benefits", "tags", "research_status", "research_priority",
    "research_notes", "duplicate_count",
]


def _safe_filename(name: str | None, fmt: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    if not name:
        return f"job-research-{stamp}.{fmt}"
    clean = re.sub(r"[^A-Za-z0-9._-]", "-", name).strip("-") or "export"
    if not clean.lower().endswith(f".{fmt}"):
        clean = f"{clean}.{fmt}"
    return clean


def _rows_for_export(db: Session, req: ExportRequest) -> list[dict]:
    stmt, _ = build_query(db, req.query.model_dump())
    jobs = db.execute(stmt.limit(req.max_rows)).scalars().all()
    if not jobs:
        return []

    job_ids = [j.id for j in jobs]

    benefits: dict[int, list[str]] = {}
    for jid, benefit in db.query(JobBenefit.job_id, JobBenefit.benefit) \
            .filter(JobBenefit.job_id.in_(job_ids)).all():
        benefits.setdefault(jid, []).append(benefit)

    custom_map: dict[int, dict] = {}
    fields: list[CustomField] = []
    if req.include_custom_fields:
        fields = db.query(CustomField).filter(
            CustomField.entity == "job").order_by(CustomField.sort_order).all()
        if fields:
            by_id = {f.id: f for f in fields}
            rows = (db.query(CustomFieldValue)
                    .filter(CustomFieldValue.entity == "job")
                    .filter(CustomFieldValue.entity_id.in_(job_ids)).all())
            for row in rows:
                field = by_id.get(row.field_id)
                if field:
                    custom_map.setdefault(row.entity_id, {})[field.key] = \
                        read_value(field, row)

    out = []
    for job in jobs:
        research = job.research
        record = {
            "id": job.id,
            "title": job.title,
            "company": job.company_name_raw,
            "location": job.location_raw,
            "city": job.city,
            "state": job.state,
            "country": job.country,
            "work_arrangement": job.work_arrangement,
            "employment_type": job.employment_type,
            "experience_level": job.experience_level,
            "education_level": job.education_level,
            "years_experience_min": job.years_experience_min,
            "salary_min": job.salary_min,
            "salary_max": job.salary_max,
            "salary_period": job.salary_period,
            "salary_annualized_min": job.salary_annualized_min,
            "salary_annualized_max": job.salary_annualized_max,
            "salary_is_estimated": bool(job.salary_is_estimated),
            "visa_sponsorship_mentioned": job.visa_sponsorship_mentioned,
            "relocation_mentioned": job.relocation_mentioned,
            "urgently_hiring": job.urgently_hiring,
            "date_posted": job.date_posted,
            "date_collected": job.date_collected,
            "source": job.source.key if job.source else None,
            "job_url": job.job_url,
            "opportunity_score": job.opportunity_score,
            "skills": "; ".join(js.skill.name for js in (job.skills or [])
                                if js.skill),
            "benefits": "; ".join(benefits.get(job.id, [])),
            "tags": "; ".join(job_tags(db, job.id)),
            "research_status": research.status if research else "new",
            "research_priority": research.priority if research else None,
            "research_notes": research.notes if research else None,
            "duplicate_count": job.duplicate_count or 0,
        }
        if req.include_description:
            record["description"] = job.description_text
        for field in fields:
            record[f"cf_{field.key}"] = custom_map.get(job.id, {}).get(field.key)
        out.append(record)
    return out


@router.post("/exports")
def create_export(req: ExportRequest, db: Session = Depends(get_db)):
    """Exports exactly what the current filters select -- not the whole DB."""
    rows = _rows_for_export(db, req)
    if not rows:
        raise ExportError(
            "There is nothing to export -- the current filters match no jobs.",
            hint="Widen the filters, or collect more data first.")

    export_dir = Path(get_setting(db, "export_dir", str(cfg.export_dir)))
    try:
        export_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ExportError(f"Could not create the export folder: {export_dir}",
                          hint="Pick a different export location in Settings."
                          ) from exc

    filename = _safe_filename(req.filename, req.format)
    path = export_dir / filename

    try:
        if req.format == "json":
            path.write_text(json.dumps(rows, indent=2, default=str),
                            encoding="utf-8")
        else:
            frame = pd.DataFrame(rows)
            for col in frame.columns:
                if frame[col].dtype == "object":
                    frame[col] = frame[col].apply(
                        lambda v: v.replace(tzinfo=None)
                        if isinstance(v, datetime) and v.tzinfo else v)
            if req.format == "csv":
                frame.to_csv(path, index=False, encoding="utf-8-sig")
            else:
                with pd.ExcelWriter(path, engine="openpyxl") as writer:
                    frame.to_excel(writer, index=False, sheet_name="Jobs")
                    sheet = writer.sheets["Jobs"]
                    for idx, col in enumerate(frame.columns, start=1):
                        width = min(60, max(12, int(
                            frame[col].astype(str).str.len().quantile(0.9) or 12)))
                        sheet.column_dimensions[
                            sheet.cell(row=1, column=idx).column_letter
                        ].width = width
                    sheet.freeze_panes = "A2"
    except OSError as exc:
        raise ExportError(f"Could not write the export file: {exc}",
                          hint="Check the file is not open in another program."
                          ) from exc

    db.add(ExportLog(format=req.format, row_count=len(rows),
                     file_path=str(path),
                     filters_json=json.dumps(req.query.model_dump(),
                                             default=str)))
    db.commit()
    log.info("Exported %d rows to %s", len(rows), path)

    return {"rows": len(rows), "file": str(path), "filename": filename,
            "download_url": f"/api/exports/download/{filename}"}


@router.get("/exports/download/{filename}")
def download_export(filename: str, db: Session = Depends(get_db)):
    export_dir = Path(get_setting(db, "export_dir", str(cfg.export_dir)))
    path = (export_dir / filename).resolve()
    if not str(path).startswith(str(export_dir.resolve())) or not path.exists():
        raise NotFoundError("That export file was not found.",
                            hint="It may have been moved or deleted.")
    return FileResponse(path, filename=filename,
                        media_type="application/octet-stream")


@router.get("/exports")
def export_history(limit: int = 50, db: Session = Depends(get_db)):
    rows = (db.query(ExportLog).order_by(ExportLog.created_at.desc())
            .limit(min(limit, 500)).all())
    return [{"id": r.id, "format": r.format, "rows": r.row_count,
             "file_path": r.file_path, "created_at": r.created_at,
             "filename": Path(r.file_path).name if r.file_path else None,
             "exists": bool(r.file_path and Path(r.file_path).exists())}
            for r in rows]


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------
SECRET_KEYS = {"adzuna_app_key", "usajobs_api_key"}


@router.get("/settings")
def read_settings(db: Session = Depends(get_db)):
    from ..models import AppSetting
    out = {}
    for row in db.query(AppSetting).all():
        try:
            value = json.loads(row.value_json or "null")
        except json.JSONDecodeError:
            value = None
        if row.key in SECRET_KEYS and value:
            value = "•" * 8          # never echo a secret back to the browser
        out[row.key] = value
    out["_paths"] = {
        "database": str(cfg.db_path),
        "logs": str(cfg.log_dir),
        "exports": get_setting(db, "export_dir", str(cfg.export_dir)),
    }
    return out


@router.put("/settings")
def update_settings(payload: SettingUpdate, db: Session = Depends(get_db)):
    changed = []
    for key, value in payload.values.items():
        if key.startswith("_"):
            continue
        if key in SECRET_KEYS and isinstance(value, str) and set(value) == {"•"}:
            continue                 # unchanged masked secret
        set_setting(db, key, value)
        changed.append(key)
    db.commit()

    if any(k.startswith(("adzuna", "usajobs")) for k in changed):
        for row in db.query(Source).filter(
                Source.requires_credentials.is_(True)).all():
            if row.health == "not_configured":
                row.health = "unknown"
        db.commit()

    return {"updated": changed}


@router.get("/settings/skills")
def list_taxonomy(db: Session = Depends(get_db)):
    rows = db.query(Skill).order_by(Skill.category, Skill.name).all()
    grouped: dict[str, list] = {}
    for skill in rows:
        try:
            aliases = json.loads(skill.aliases_json or "[]")
        except json.JSONDecodeError:
            aliases = []
        grouped.setdefault(skill.category or "other", []).append(
            {"id": skill.id, "name": skill.name, "aliases": aliases})
    return grouped


@router.post("/settings/skills")
def add_skill(name: str, category: str = "custom", aliases: str = "",
              db: Session = Depends(get_db)):
    if db.query(Skill).filter(Skill.name == name).first():
        raise ExportError(f"'{name}' is already in the taxonomy.",
                          hint="Add an alias to the existing entry instead.")
    alias_list = [a.strip() for a in aliases.split(",") if a.strip()]
    skill = Skill(name=name, category=category,
                  aliases_json=json.dumps(alias_list))
    db.add(skill)
    db.commit()
    invalidate_matcher()
    return {"id": skill.id, "name": name,
            "note": "Re-run extraction or collect again to apply it to existing jobs."}


@router.delete("/settings/skills/{skill_id}")
def delete_skill(skill_id: int, db: Session = Depends(get_db)):
    skill = db.get(Skill, skill_id)
    if skill is None:
        raise NotFoundError("That skill is not in the taxonomy.")
    db.delete(skill)
    db.commit()
    invalidate_matcher()
    return {"deleted": skill_id}


@router.get("/settings/logs")
def tail_log(lines: int = 200):
    path = cfg.log_dir / "app.log"
    if not path.exists():
        return {"lines": [], "path": str(path)}
    try:
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        raise ExportError(f"Could not read the log file: {exc}") from exc
    return {"lines": content[-min(lines, 2000):], "path": str(path)}
