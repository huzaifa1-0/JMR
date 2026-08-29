"""Normalisation helpers.

Sources disagree about almost everything -- how they spell a company, how they
express salary, what they call a full-time job.  Everything is coerced here so
the rest of the application only ever sees one vocabulary.
"""
from __future__ import annotations

import html
import re
from datetime import datetime, timezone

from dateutil import parser as dateparser

# --------------------------------------------------------------------------
# Text
# --------------------------------------------------------------------------
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_NL_RE = re.compile(r"\n{3,}")


def html_to_text(raw: str | None) -> str:
    if not raw:
        return ""
    text = re.sub(r"<\s*(br|/p|/div|/li|/h[1-6])\s*/?>", "\n", raw,
                  flags=re.I)
    text = re.sub(r"<\s*li[^>]*>", "\n- ", text, flags=re.I)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = _WS_RE.sub(" ", text)
    text = _NL_RE.sub("\n\n", text)
    return text.strip()


_COMPANY_SUFFIXES = {
    "inc", "inc.", "llc", "l.l.c.", "ltd", "ltd.", "limited", "corp",
    "corp.", "corporation", "co", "co.", "company", "plc", "gmbh", "sa",
    "sas", "bv", "nv", "ag", "pty", "pte", "llp", "lp", "pc", "pllc",
    "group", "holdings", "holding", "the",
}


def normalize_company_name(name: str | None) -> str:
    if not name:
        return ""
    s = name.lower().strip()
    s = re.sub(r"[\u2018\u2019\u201c\u201d']", "", s)
    s = re.sub(r"[^a-z0-9&\s.-]", " ", s)
    tokens = [t for t in re.split(r"[\s,]+", s) if t]
    tokens = [t for t in tokens if t.strip(".") not in _COMPANY_SUFFIXES]
    return " ".join(tokens).strip() or name.lower().strip()


_TITLE_NOISE = re.compile(
    r"\b(urgent(ly)?\s+hiring|hiring\s+now|immediate\s+start|apply\s+now|"
    r"remote|work\s+from\s+home|w2|1099|full[\s-]?time|part[\s-]?time)\b",
    re.I,
)


def normalize_title(title: str | None) -> str:
    if not title:
        return ""
    s = title.lower()
    s = re.sub(r"[\(\[\{].*?[\)\]\}]", " ", s)
    s = _TITLE_NOISE.sub(" ", s)
    s = re.sub(r"[^a-z0-9+#/\s-]", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip(" -/")


# --------------------------------------------------------------------------
# Location
# --------------------------------------------------------------------------
US_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT",
    "delaware": "DE", "district of columbia": "DC", "florida": "FL",
    "georgia": "GA", "hawaii": "HI", "idaho": "ID", "illinois": "IL",
    "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY",
    "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT",
    "nebraska": "NE", "nevada": "NV", "new hampshire": "NH",
    "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH",
    "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD",
    "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY", "puerto rico": "PR",
}
STATE_ABBRS = set(US_STATES.values())
_ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\b")


def parse_location(raw: str | None) -> dict:
    """Best-effort split of a free-text location into components."""
    out = {"city": None, "state": None, "country": "US", "postal_code": None,
           "location_raw": raw}
    if not raw:
        return out

    s = raw.strip()
    zip_match = _ZIP_RE.search(s)
    if zip_match:
        out["postal_code"] = zip_match.group(1)
        s = _ZIP_RE.sub("", s)

    s = re.sub(r"\b(united states of america|united states|usa|u\.s\.a\.|u\.s\.)\b",
               "", s, flags=re.I)
    parts = [p.strip(" ,-") for p in s.split(",") if p.strip(" ,-")]

    if not parts:
        return out

    # state may be the last part
    tail = parts[-1]
    if tail.upper() in STATE_ABBRS:
        out["state"] = tail.upper()
        parts = parts[:-1]
    elif tail.lower() in US_STATES:
        out["state"] = US_STATES[tail.lower()]
        parts = parts[:-1]
    elif len(tail) > 3 and tail.lower() not in ("remote", "anywhere"):
        # possibly a non-US country
        if tail.lower() not in US_STATES and not re.match(r"^[A-Z]{2}$", tail):
            pass

    if parts:
        city = parts[0]
        if city.lower() not in ("remote", "anywhere", "united states", "us"):
            out["city"] = city
    return out


def detect_work_arrangement(*texts: str | None) -> str:
    blob = " ".join(t for t in texts if t).lower()
    if not blob:
        return "unknown"
    if re.search(r"\bhybrid\b", blob):
        return "hybrid"
    if re.search(r"\b(fully remote|100% remote|remote[- ]first|work from home|"
                 r"telecommut\w*|wfh|remote)\b", blob):
        # "remote" sometimes appears as "no remote work" -- check for negation
        if re.search(r"\b(no|not|non)[- ]remote\b", blob):
            return "onsite"
        return "remote"
    if re.search(r"\b(on[- ]site|onsite|in[- ]office|in[- ]person)\b", blob):
        return "onsite"
    return "unknown"


# --------------------------------------------------------------------------
# Employment type / experience level
# --------------------------------------------------------------------------
EMPLOYMENT_TYPES = ["full_time", "part_time", "contract", "temporary",
                    "internship"]

_EMPLOYMENT_PATTERNS = [
    ("internship", r"\b(intern(ship)?|co[- ]op)\b"),
    ("temporary", r"\b(temporary|temp\b|seasonal|locum)\b"),
    ("contract", r"\b(contract(or)?|freelance|1099|c2c|corp[- ]to[- ]corp|"
                 r"contract[- ]to[- ]hire)\b"),
    ("part_time", r"\b(part[\s-]?time|prn\b)\b"),
    ("full_time", r"\b(full[\s-]?time|permanent|regular)\b"),
]


def detect_employment_type(*texts: str | None) -> str | None:
    blob = " ".join(t for t in texts if t).lower()
    if not blob:
        return None
    for label, pattern in _EMPLOYMENT_PATTERNS:
        if re.search(pattern, blob):
            return label
    return None


EXPERIENCE_LEVELS = ["entry", "mid", "senior", "manager", "director",
                     "executive"]

_LEVEL_PATTERNS = [
    ("executive", r"\b(chief|c[toefi]o\b|vp\b|vice president|president|"
                  r"executive vice)\b"),
    ("director", r"\b(director|head of)\b"),
    ("manager", r"\b(manager|supervisor|team lead|team leader)\b"),
    ("senior", r"\b(senior|sr\.?|staff|principal|lead|iii|iv)\b"),
    ("entry", r"\b(entry[- ]level|junior|jr\.?|associate|trainee|graduate|"
              r"apprentice|intern)\b"),
]


def detect_experience_level(title: str | None, description: str | None = None,
                            years_min: int | None = None) -> str | None:
    blob = (title or "").lower()
    for label, pattern in _LEVEL_PATTERNS:
        if re.search(pattern, blob):
            return label
    if years_min is not None:
        if years_min >= 8:
            return "senior"
        if years_min >= 3:
            return "mid"
        if years_min >= 0:
            return "entry"
    if description:
        for label, pattern in _LEVEL_PATTERNS:
            if re.search(pattern, description[:1500].lower()):
                return label
    return None


# --------------------------------------------------------------------------
# Salary
# --------------------------------------------------------------------------
PERIOD_MULTIPLIERS = {
    "hourly": 2080.0,
    "daily": 260.0,
    "weekly": 52.0,
    "biweekly": 26.0,
    "monthly": 12.0,
    "yearly": 1.0,
    "annual": 1.0,
}

_SALARY_RE = re.compile(
    r"\$\s?(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s?([kK])?"
    r"(?:\s?(?:-|--|to|through|\u2013)\s?"
    r"\$?\s?(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s?([kK])?)?"
)

_PERIOD_HINTS = [
    ("hourly", r"\b(per hour|/\s?hour|/\s?hr|an hour|hourly|p/h)\b"),
    ("weekly", r"\b(per week|/\s?week|weekly)\b"),
    ("monthly", r"\b(per month|/\s?month|monthly|per mo)\b"),
    ("yearly", r"\b(per year|/\s?year|annually|annual|per annum|/\s?yr|a year)\b"),
]


def _to_float(num: str, k_suffix: str | None) -> float:
    val = float(num.replace(",", ""))
    if k_suffix:
        val *= 1000
    return val


def guess_period(value: float | None, text: str) -> str:
    low = text.lower()
    for label, pattern in _PERIOD_HINTS:
        if re.search(pattern, low):
            return label
    if value is None:
        return "yearly"
    if value < 200:
        return "hourly"
    if value < 2000:
        return "weekly"
    if value < 25000:
        return "monthly"
    return "yearly"


def parse_salary_from_text(text: str | None) -> dict | None:
    """Pull a salary range out of free text. Returns None if nothing credible."""
    if not text:
        return None
    window = text[:6000]
    match = _SALARY_RE.search(window)
    if not match:
        return None
    lo = _to_float(match.group(1), match.group(2))
    hi = _to_float(match.group(3), match.group(4)) if match.group(3) else None
    context = window[max(0, match.start() - 60): match.end() + 60]
    period = guess_period(lo, context)
    if hi is not None and hi < lo:
        lo, hi = hi, lo
    return {"salary_min": lo, "salary_max": hi, "salary_period": period,
            "salary_currency": "USD"}


def annualize(value: float | None, period: str | None) -> float | None:
    if value is None:
        return None
    mult = PERIOD_MULTIPLIERS.get((period or "yearly").lower(), 1.0)
    return round(value * mult, 2)


def normalize_salary(salary_min: float | None, salary_max: float | None,
                     period: str | None, currency: str | None = "USD") -> dict:
    period = (period or "yearly").lower()
    if period in ("annual", "year", "yearly", "per_year"):
        period = "yearly"
    return {
        "salary_min": salary_min,
        "salary_max": salary_max,
        "salary_period": period,
        "salary_currency": currency or "USD",
        "salary_annualized_min": annualize(salary_min, period),
        "salary_annualized_max": annualize(salary_max, period),
    }


# --------------------------------------------------------------------------
# Dates
# --------------------------------------------------------------------------
def parse_date(value) -> datetime | None:
    if value in (None, "", 0):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        dt = dateparser.parse(str(value))
    except (ValueError, OverflowError, TypeError):
        return None
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def days_since(dt: datetime | None) -> int | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0, (datetime.now(timezone.utc) - dt).days)
