"""Extraction of structured facts from free-text job descriptions.

Deliberately deterministic: a gazetteer plus regular expressions, no model
download, no network call, no per-request cost.  The skills vocabulary lives in
data/taxonomies/skills.yaml and is editable from the Settings UI, so coverage
improves without touching code.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache

from sqlalchemy.orm import Session

from ..models import Skill

# --------------------------------------------------------------------------
# Skills
# --------------------------------------------------------------------------
_WORD_BOUNDARY_SAFE = re.compile(r"^[\w+#./ -]+$")


@lru_cache(maxsize=1)
def _compiled_cache_key() -> int:
    return 0


class SkillMatcher:
    """Alias -> canonical skill lookup, compiled into one alternation regex."""

    def __init__(self, entries: list[tuple[int, str, list[str]]]):
        self.alias_to_id: dict[str, int] = {}
        self.id_to_name: dict[int, str] = {}
        patterns: list[str] = []
        for skill_id, name, aliases in entries:
            self.id_to_name[skill_id] = name
            for alias in [name, *aliases]:
                alias_l = alias.lower().strip()
                if not alias_l or not _WORD_BOUNDARY_SAFE.match(alias_l):
                    continue
                self.alias_to_id.setdefault(alias_l, skill_id)
                patterns.append(re.escape(alias_l))
        patterns.sort(key=len, reverse=True)
        self.regex = (
            re.compile(r"(?<![\w+#])(" + "|".join(patterns) + r")(?![\w+#])")
            if patterns else None
        )

    def find(self, text: str) -> dict[int, int]:
        """Return {skill_id: mention_count}."""
        if not text or self.regex is None:
            return {}
        counts: dict[int, int] = {}
        for match in self.regex.finditer(text.lower()):
            skill_id = self.alias_to_id.get(match.group(1))
            if skill_id is not None:
                counts[skill_id] = counts.get(skill_id, 0) + 1
        return counts


_matcher: SkillMatcher | None = None


def build_matcher(db: Session, force: bool = False) -> SkillMatcher:
    global _matcher
    if _matcher is not None and not force:
        return _matcher
    entries = []
    for skill in db.query(Skill).all():
        try:
            aliases = json.loads(skill.aliases_json or "[]")
        except json.JSONDecodeError:
            aliases = []
        entries.append((skill.id, skill.name, aliases))
    _matcher = SkillMatcher(entries)
    return _matcher


def invalidate_matcher() -> None:
    global _matcher
    _matcher = None


# Requirement classification -------------------------------------------------
_REQUIRED_HEADINGS = re.compile(
    r"(required|requirements|must have|qualifications|you (will )?need|"
    r"minimum qualifications|essential)", re.I)
_PREFERRED_HEADINGS = re.compile(
    r"(preferred|nice to have|bonus|plus|desirable|a plus|advantageous)", re.I)


def _section_for_offset(text: str, offset: int) -> str:
    """Look backwards for the nearest heading to classify a mention."""
    window = text[max(0, offset - 700): offset]
    pref = _PREFERRED_HEADINGS.search(window)
    req = _REQUIRED_HEADINGS.search(window)
    if pref and (not req or pref.start() > req.start()):
        return "preferred"
    if req:
        return "required"
    return "mentioned"


def extract_skills(db: Session, text: str) -> list[dict]:
    """Return [{skill_id, name, requirement, mention_count}]."""
    if not text:
        return []
    matcher = build_matcher(db)
    if matcher.regex is None:
        return []
    lowered = text.lower()
    hits: dict[int, dict] = {}
    for match in matcher.regex.finditer(lowered):
        skill_id = matcher.alias_to_id.get(match.group(1))
        if skill_id is None:
            continue
        entry = hits.setdefault(skill_id, {
            "skill_id": skill_id,
            "name": matcher.id_to_name[skill_id],
            "requirement": "mentioned",
            "mention_count": 0,
        })
        entry["mention_count"] += 1
        if entry["requirement"] == "mentioned":
            entry["requirement"] = _section_for_offset(text, match.start())
    return list(hits.values())


# --------------------------------------------------------------------------
# Years of experience
# --------------------------------------------------------------------------
_YEARS_RANGE = re.compile(
    r"(\d{1,2})\s*(?:\+|plus)?\s*(?:-|to|\u2013)\s*(\d{1,2})\s*\+?\s*"
    r"(?:years?|yrs?)", re.I)
_YEARS_MIN = re.compile(
    r"(?:(?:at least|minimum(?: of)?|min\.?|over|more than)\s*)?"
    r"(\d{1,2})\s*\+?\s*(?:years?|yrs?)", re.I)
_WORD_YEARS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}
_WORD_YEARS_RE = re.compile(
    r"\b(" + "|".join(_WORD_YEARS) + r")\s*\+?\s*(?:years?|yrs?)", re.I)


def extract_experience(text: str | None) -> dict:
    out = {"years_experience_min": None, "years_experience_max": None}
    if not text:
        return out
    window = text[:8000]

    m = _YEARS_RANGE.search(window)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        if 0 <= lo <= 40 and lo <= hi <= 40:
            out["years_experience_min"] = lo
            out["years_experience_max"] = hi
            return out

    candidates = [int(x) for x in _YEARS_MIN.findall(window)
                  if 0 < int(x) <= 30]
    if not candidates:
        wm = _WORD_YEARS_RE.search(window)
        if wm:
            candidates = [_WORD_YEARS[wm.group(1).lower()]]
    if candidates:
        out["years_experience_min"] = min(candidates)
    return out


# --------------------------------------------------------------------------
# Education
# --------------------------------------------------------------------------
_EDUCATION_LEVELS = [
    ("doctorate", r"\b(ph\.?d|doctorate|doctoral)\b"),
    # Postings very often write "Master's preferred" with no "degree" after it,
    # so the qualifier word is optional rather than required.
    ("masters", r"\b(master'?s?(\s+degree)?\b|m\.?s\.?c?\b|m\.?b\.?a\b|"
                r"master of|graduate degree)\b"),
    ("bachelors", r"\b(bachelor'?s?(\s+degree)?\b|b\.?s\.?c?\b|b\.?a\b|"
                  r"undergraduate degree|bachelor of|four[- ]year degree)\b"),
    ("associates", r"\b(associate'?s?\s+degree|a\.?a\.?s?\b|two[- ]year degree)\b"),
    ("certification", r"\b(certification required|certified|cpc\b|ccs\b|"
                      r"rhia\b|rhit\b)\b"),
    ("high_school", r"\b(high school diploma|ged\b|hs diploma)\b"),
]


def extract_education(text: str | None) -> str | None:
    if not text:
        return None
    low = text[:8000].lower()
    for label, pattern in _EDUCATION_LEVELS:
        if re.search(pattern, low):
            return label
    return None


# --------------------------------------------------------------------------
# Benefits
# --------------------------------------------------------------------------
BENEFIT_PATTERNS = [
    ("health_insurance", r"\b(health insurance|medical insurance|health benefits|"
                         r"medical, dental|health coverage)\b"),
    ("dental", r"\bdental\b"),
    ("vision", r"\bvision (insurance|coverage|plan)\b"),
    ("retirement_401k", r"\b(401\s?\(?k\)?|403\s?\(?b\)?|retirement plan|pension)\b"),
    ("paid_time_off", r"\b(pto\b|paid time off|vacation days|paid vacation|"
                      r"annual leave)\b"),
    ("paid_holidays", r"\bpaid holidays?\b"),
    ("parental_leave", r"\b(parental leave|maternity leave|paternity leave)\b"),
    ("remote_stipend", r"\b(home office stipend|remote work stipend|"
                       r"internet reimbursement)\b"),
    ("tuition_reimbursement", r"\b(tuition (reimbursement|assistance)|"
                              r"education (reimbursement|assistance))\b"),
    ("bonus", r"\b(annual bonus|performance bonus|signing bonus|bonus eligible)\b"),
    ("equity", r"\b(equity|stock options|rsus?\b|espp\b)\b"),
    ("life_insurance", r"\blife insurance\b"),
    ("disability", r"\b(short[- ]term disability|long[- ]term disability|std/ltd)\b"),
    ("flexible_schedule", r"\b(flexible (schedule|hours)|flex time|flextime)\b"),
    ("professional_development", r"\b(professional development|learning budget|"
                                 r"conference budget)\b"),
]


def extract_benefits(text: str | None) -> list[str]:
    if not text:
        return []
    low = text.lower()
    return [label for label, pattern in BENEFIT_PATTERNS
            if re.search(pattern, low)]


# --------------------------------------------------------------------------
# Flags
# --------------------------------------------------------------------------
_SPONSOR_POSITIVE = re.compile(
    r"\b(visa sponsorship (is )?(available|provided|offered)|will sponsor|"
    r"we sponsor|sponsorship available|h1[- ]?b sponsorship|"
    r"open to sponsorship|able to sponsor)\b", re.I)
_SPONSOR_NEGATIVE = re.compile(
    r"\b(no (visa )?sponsorship|unable to sponsor|cannot sponsor|"
    r"not able to sponsor|do(es)? not (provide|offer) sponsorship|"
    r"sponsorship is not available|without sponsorship|"
    r"not (currently )?sponsor)\b", re.I)

_RELOCATION = re.compile(
    r"\b(relocation (assistance|package|support|reimbursement|bonus)|"
    r"will relocate|relocation offered|relocation provided)\b", re.I)
_RELOCATION_NEG = re.compile(
    r"\bno relocation( assistance| package)?\b", re.I)

_URGENT = re.compile(
    r"\b(urgent(ly)? hiring|immediate (start|opening|hire|need)|"
    r"start (immediately|asap)|asap\b|hiring now|"
    r"multiple openings|filling quickly)\b", re.I)

_AGENCY = re.compile(
    r"\b(staffing|recruit(ing|ment) (agency|firm|partner)|talent (partner|"
    r"acquisition firm)|on behalf of our client|our client is (seeking|"
    r"looking)|search firm|headhunt)\b", re.I)

_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]{2,}\b")
_PHONE_RE = re.compile(r"\b(?:\+1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}\b")


def extract_flags(text: str | None, title: str | None = None) -> dict:
    blob = f"{title or ''}\n{text or ''}"
    sponsorship = None
    if _SPONSOR_NEGATIVE.search(blob):
        sponsorship = False
    elif _SPONSOR_POSITIVE.search(blob):
        sponsorship = True

    relocation = None
    if _RELOCATION_NEG.search(blob):
        relocation = False
    elif _RELOCATION.search(blob):
        relocation = True

    return {
        "visa_sponsorship_mentioned": sponsorship,
        "relocation_mentioned": relocation,
        "urgently_hiring": bool(_URGENT.search(blob)) or None,
        "is_recruitment_agency": bool(_AGENCY.search(blob)),
    }


def extract_contacts(text: str | None) -> dict:
    """Publicly stated contact details inside the posting body only."""
    if not text:
        return {"emails": [], "phones": []}
    return {
        "emails": sorted(set(_EMAIL_RE.findall(text)))[:5],
        "phones": sorted(set(_PHONE_RE.findall(text)))[:5],
    }
