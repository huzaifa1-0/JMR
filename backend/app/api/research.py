"""Research layer: notes, status, tags, priority, and custom fields."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.errors import AppError, NotFoundError
from ..db import get_db
from ..models import (CustomField, CustomFieldValue, Job, JobTag, Research,
                      ScoringRule, Tag)
from ..processing.scoring import rescore_jobs
from ..schemas import (BulkResearchUpdate, CustomFieldCreate, CustomFieldOut,
                       CustomFieldValueSet, ResearchUpdate, ScoringRuleIn)
from .serializers import custom_values_for, write_value

router = APIRouter()

STATUSES = ["new", "reviewed", "relevant", "not_relevant", "follow_up",
            "archived"]


# --------------------------------------------------------------------------
# Per-job research
# --------------------------------------------------------------------------
def _get_or_create(db: Session, job_id: int) -> Research:
    if db.get(Job, job_id) is None:
        raise NotFoundError(f"Job {job_id} was not found.")
    row = db.query(Research).filter(Research.job_id == job_id).first()
    if row is None:
        row = Research(job_id=job_id, status="new")
        db.add(row)
        db.flush()
    return row


def _set_tags(db: Session, job_id: int, names: list[str]) -> None:
    db.query(JobTag).filter(JobTag.job_id == job_id).delete()
    for name in {n.strip() for n in names if n and n.strip()}:
        tag = db.query(Tag).filter(Tag.name == name).first()
        if tag is None:
            tag = Tag(name=name)
            db.add(tag)
            db.flush()
        db.add(JobTag(job_id=job_id, tag_id=tag.id))


@router.get("/research/{job_id}")
def get_research(job_id: int, db: Session = Depends(get_db)):
    row = db.query(Research).filter(Research.job_id == job_id).first()
    tags = [r[0] for r in db.query(Tag.name)
            .join(JobTag, JobTag.tag_id == Tag.id)
            .filter(JobTag.job_id == job_id).all()]
    return {
        "job_id": job_id,
        "status": row.status if row else "new",
        "priority": row.priority if row else None,
        "lead_quality": row.lead_quality if row else None,
        "notes": row.notes if row else None,
        "follow_up_at": row.follow_up_at if row else None,
        "tags": tags,
        "custom_fields": custom_values_for(db, "job", job_id),
    }


@router.put("/research/{job_id}")
def update_research(job_id: int, payload: ResearchUpdate,
                    db: Session = Depends(get_db)):
    row = _get_or_create(db, job_id)
    data = payload.model_dump(exclude_unset=True)
    tags = data.pop("tags", None)
    for key, value in data.items():
        setattr(row, key, value)
    if tags is not None:
        _set_tags(db, job_id, tags)
    db.commit()
    return get_research(job_id, db)


@router.post("/research/bulk")
def bulk_update(payload: BulkResearchUpdate, db: Session = Depends(get_db)):
    touched = 0
    for job_id in payload.job_ids:
        if db.get(Job, job_id) is None:
            continue
        row = _get_or_create(db, job_id)
        if payload.status:
            row.status = payload.status
        if payload.priority:
            row.priority = payload.priority
        if payload.add_tags:
            existing = [r[0] for r in db.query(Tag.name)
                        .join(JobTag, JobTag.tag_id == Tag.id)
                        .filter(JobTag.job_id == job_id).all()]
            _set_tags(db, job_id, list(set(existing) | set(payload.add_tags)))
        if payload.remove_tags:
            existing = [r[0] for r in db.query(Tag.name)
                        .join(JobTag, JobTag.tag_id == Tag.id)
                        .filter(JobTag.job_id == job_id).all()]
            _set_tags(db, job_id,
                      [t for t in existing if t not in payload.remove_tags])
        touched += 1
    db.commit()
    return {"updated": touched}


@router.get("/research/summary/status")
def status_summary(db: Session = Depends(get_db)):
    rows = db.execute(
        select(Research.status, func.count()).group_by(Research.status)
    ).all()
    counts = {status: 0 for status in STATUSES}
    for status, n in rows:
        counts[status or "new"] = n
    unreviewed = db.execute(
        select(func.count()).select_from(Job)
        .where(Job.is_primary.is_(True))
        .where(~select(Research.job_id).where(Research.job_id == Job.id).exists())
    ).scalar_one()
    counts["new"] += unreviewed
    return counts


# --------------------------------------------------------------------------
# Tags
# --------------------------------------------------------------------------
@router.get("/tags")
def list_tags(db: Session = Depends(get_db)):
    rows = db.execute(
        select(Tag.name, Tag.color, func.count(JobTag.job_id))
        .join(JobTag, JobTag.tag_id == Tag.id, isouter=True)
        .group_by(Tag.id).order_by(func.count(JobTag.job_id).desc())
    ).all()
    return [{"name": r[0], "color": r[1], "count": r[2]} for r in rows]


@router.delete("/tags/{name}")
def delete_tag(name: str, db: Session = Depends(get_db)):
    tag = db.query(Tag).filter(Tag.name == name).first()
    if tag is None:
        raise NotFoundError(f"No tag named '{name}'.")
    db.query(JobTag).filter(JobTag.tag_id == tag.id).delete()
    db.delete(tag)
    db.commit()
    return {"deleted": name}


# --------------------------------------------------------------------------
# Custom fields
# --------------------------------------------------------------------------
def _field_out(f: CustomField) -> CustomFieldOut:
    try:
        options = json.loads(f.options_json or "[]")
    except json.JSONDecodeError:
        options = []
    return CustomFieldOut(
        id=f.id, key=f.key, label=f.label, entity=f.entity,
        field_type=f.field_type, options=options,
        default_value=f.default_value, is_filterable=bool(f.is_filterable),
        sort_order=f.sort_order or 0)


@router.get("/custom-fields", response_model=list[CustomFieldOut])
def list_fields(entity: str | None = None, db: Session = Depends(get_db)):
    q = db.query(CustomField)
    if entity:
        q = q.filter(CustomField.entity == entity)
    return [_field_out(f) for f in q.order_by(CustomField.sort_order,
                                              CustomField.id).all()]


@router.post("/custom-fields", response_model=CustomFieldOut)
def create_field(payload: CustomFieldCreate, db: Session = Depends(get_db)):
    if db.query(CustomField).filter(CustomField.key == payload.key).first():
        raise AppError(f"A custom field with the key '{payload.key}' already exists.",
                       hint="Choose a different key, or edit the existing field.")
    if payload.field_type in ("dropdown", "multiselect") and not payload.options:
        raise AppError(f"A {payload.field_type} field needs at least one option.",
                       hint="Add the choices you want to pick from.")
    row = CustomField(
        key=payload.key, label=payload.label, entity=payload.entity,
        field_type=payload.field_type,
        options_json=json.dumps(payload.options),
        default_value=payload.default_value,
        is_filterable=payload.is_filterable, sort_order=payload.sort_order)
    db.add(row)
    db.commit()
    return _field_out(row)


@router.delete("/custom-fields/{field_id}")
def delete_field(field_id: int, db: Session = Depends(get_db)):
    row = db.get(CustomField, field_id)
    if row is None:
        raise NotFoundError("That custom field no longer exists.")
    db.query(CustomFieldValue).filter(
        CustomFieldValue.field_id == field_id).delete()
    db.delete(row)
    db.commit()
    return {"deleted": field_id}


@router.put("/custom-fields/{key}/value")
def set_field_value(key: str, payload: CustomFieldValueSet,
                    db: Session = Depends(get_db)):
    field = db.query(CustomField).filter(CustomField.key == key).first()
    if field is None:
        raise NotFoundError(f"No custom field with the key '{key}'.")

    row = (db.query(CustomFieldValue)
           .filter(CustomFieldValue.field_id == field.id)
           .filter(CustomFieldValue.entity == payload.entity)
           .filter(CustomFieldValue.entity_id == payload.entity_id).first())
    if row is None:
        row = CustomFieldValue(field_id=field.id, entity=payload.entity,
                               entity_id=payload.entity_id)
        db.add(row)

    try:
        write_value(field, row, payload.value)
    except (TypeError, ValueError) as exc:
        raise AppError(
            f"'{payload.value}' is not a valid {field.field_type} value.",
            hint="Check the field type in Settings.") from exc

    db.commit()
    return {"key": key, "entity_id": payload.entity_id, "value": payload.value}


# --------------------------------------------------------------------------
# Scoring rules
# --------------------------------------------------------------------------
@router.get("/scoring-rules")
def list_rules(db: Session = Depends(get_db)):
    return [{
        "id": r.id, "name": r.name, "entity": r.entity,
        "is_active": bool(r.is_active), "weight": r.weight, "points": r.points,
        "condition": json.loads(r.condition_json),
    } for r in db.query(ScoringRule).order_by(ScoringRule.id).all()]


@router.post("/scoring-rules")
def upsert_rule(payload: ScoringRuleIn, db: Session = Depends(get_db)):
    row = db.get(ScoringRule, payload.id) if payload.id else None
    if row is None:
        row = ScoringRule(name=payload.name, entity=payload.entity)
        db.add(row)
    row.name = payload.name
    row.entity = payload.entity
    row.is_active = payload.is_active
    row.weight = payload.weight
    row.points = payload.points
    row.condition_json = json.dumps(payload.condition)
    db.commit()
    return {"id": row.id}


@router.delete("/scoring-rules/{rule_id}")
def delete_rule(rule_id: int, db: Session = Depends(get_db)):
    row = db.get(ScoringRule, rule_id)
    if row is None:
        raise NotFoundError("That scoring rule no longer exists.")
    db.delete(row)
    db.commit()
    return {"deleted": rule_id}


@router.post("/scoring-rules/rescore")
def rescore(db: Session = Depends(get_db)):
    return {"rescored": rescore_jobs(db)}
