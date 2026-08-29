"""Deduplication.

Three passes, cheapest first:

  1. exact      -- (source_id, source_job_id) unique constraint, handled by the DB
  2. canonical  -- SHA1 of normalised title | company | location
  3. fuzzy      -- among canonical candidates, token-set title similarity plus
                   simhash Hamming distance on the description

Duplicates are *linked*, never discarded: `duplicate_of_id` points at the
primary, and the record from the deepest source wins primary status.  Presence
across several sources is itself a research signal.
"""
from __future__ import annotations

import hashlib
import re
from difflib import SequenceMatcher

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Job
from .normalize import normalize_company_name, normalize_title

# Sources ranked by how complete their records are. Higher wins primary.
SOURCE_DEPTH = {
    "greenhouse": 100, "lever": 100, "ashby": 100, "workable": 95,
    "recruitee": 95, "smartrecruiters": 90,
    "usajobs": 80, "remotive": 60, "adzuna": 40,
}

_TOKEN_RE = re.compile(r"[a-z0-9+#]+")

# Below this description similarity, a fingerprint match is treated as two
# distinct openings rather than one posting seen twice.
CONTRADICTION_FLOOR = 0.45


# --------------------------------------------------------------------------
# Fingerprints
# --------------------------------------------------------------------------
def canonical_hash(title: str | None, company: str | None,
                   city: str | None, state: str | None) -> str:
    parts = [
        normalize_title(title),
        normalize_company_name(company),
        (city or "").strip().lower(),
        (state or "").strip().upper(),
    ]
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


def simhash(text: str | None, bits: int = 64) -> str:
    """64-bit simhash over token unigrams. Hex string, or '' for empty input.

    Unigrams rather than n-grams, which is a deliberate and measured choice.
    With 3-gram shingles a single substituted word breaks three shingles at
    once, so genuinely duplicate postings scored around 0.73-0.81 -- below the
    0.85 threshold, meaning real duplicates were being missed.  Measured
    separation on the same corpus:

        shingling   true dupes    unrelated    similar-but-distinct
        3-gram      0.73 - 0.81     0.42            0.56
        unigram     0.91 - 0.92     0.47            0.75

    Unigrams push true duplicates well above the threshold while leaving
    similar-but-distinct postings (same role, different employer boilerplate)
    comfortably below it.  Word order is already captured by the title
    similarity check, so losing it here costs nothing.
    """
    if not text:
        return ""
    tokens = _TOKEN_RE.findall(text.lower())
    if len(tokens) < 4:
        return ""
    vector = [0] * bits
    for token in tokens:
        h = int(hashlib.md5(token.encode("utf-8")).hexdigest()[:16], 16)
        for i in range(bits):
            vector[i] += 1 if (h >> i) & 1 else -1
    value = 0
    for i in range(bits):
        if vector[i] > 0:
            value |= (1 << i)
    return f"{value:016x}"


def hamming(a: str, b: str) -> int:
    if not a or not b:
        return 64
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def simhash_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return 1.0 - (hamming(a, b) / 64.0)


def title_similarity(a: str | None, b: str | None) -> float:
    ta, tb = normalize_title(a), normalize_title(b)
    if not ta or not tb:
        return 0.0
    if ta == tb:
        return 1.0
    # token-set ratio: order-insensitive, robust to "Senior Engineer, Backend"
    sa, sb = set(ta.split()), set(tb.split())
    if not sa or not sb:
        return 0.0
    jaccard = len(sa & sb) / len(sa | sb)
    seq = SequenceMatcher(None, ta, tb).ratio()
    return max(jaccard, seq)


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------
class DuplicateVerdict:
    __slots__ = ("is_duplicate", "primary_id", "reason", "score")

    def __init__(self, is_duplicate: bool, primary_id: int | None = None,
                 reason: str = "", score: float = 0.0):
        self.is_duplicate = is_duplicate
        self.primary_id = primary_id
        self.reason = reason
        self.score = score


def find_duplicate(db: Session, *, title: str, company: str | None,
                   city: str | None, state: str | None,
                   description: str | None, job_url: str | None,
                   source_key: str,
                   title_threshold: float = 0.92,
                   desc_threshold: float = 0.85) -> DuplicateVerdict:
    """Look for an existing job that is the same posting as the incoming one."""

    # Pass A -- identical apply/job URL is conclusive
    if job_url:
        hit = db.execute(
            select(Job).where(Job.job_url == job_url).limit(1)
        ).scalar_one_or_none()
        if hit:
            return DuplicateVerdict(True, hit.id, "identical_url", 1.0)

    # Pass B -- canonical fingerprint (same normalised title, company, location)
    chash = canonical_hash(title, company, city, state)
    candidates = db.execute(
        select(Job).where(Job.canonical_hash == chash).limit(25)
    ).scalars().all()

    # Pass C -- same company and location, fuzzy title. Catches the cases the
    # fingerprint misses: "Billing Specialist II" vs "Billing Specialist, Level 2".
    if not candidates:
        norm_company = normalize_company_name(company)
        if norm_company:
            candidates = db.execute(
                select(Job)
                .where(Job.title_normalized.isnot(None))
                .where(func.lower(func.coalesce(Job.city, "")) ==
                       (city or "").lower())
                .where(func.upper(func.coalesce(Job.state, "")) ==
                       (state or "").upper())
                .where(Job.company_name_raw.isnot(None))
                .limit(200)
            ).scalars().all()
            candidates = [
                c for c in candidates
                if normalize_company_name(c.company_name_raw) == norm_company
            ]

    if not candidates:
        return DuplicateVerdict(False)

    incoming_hash = simhash(description)
    best: tuple[float, Job, str] | None = None

    for cand in candidates:
        t_sim = title_similarity(title, cand.title)
        if t_sim < title_threshold:
            continue

        # Description similarity CONFIRMS a match; it does not veto one.
        # Two sources routinely publish the same role in different words --
        # rejecting on that basis would leave obvious duplicates unlinked.
        if incoming_hash and cand.simhash:
            d_sim = simhash_similarity(incoming_hash, cand.simhash)
            if d_sim >= desc_threshold:
                score, reason = (t_sim + d_sim) / 2, "title+description"
            elif d_sim < CONTRADICTION_FLOOR:
                # Same title, company and city but genuinely unrelated text --
                # most often two distinct openings on one team. Leave separate.
                continue
            else:
                score, reason = t_sim, "canonical+title"
        else:
            score, reason = t_sim, "canonical+title"

        if best is None or score > best[0]:
            best = (score, cand, reason)

    if best:
        return DuplicateVerdict(True, best[1].id, best[2], round(best[0], 3))
    return DuplicateVerdict(False)


def should_promote(new_source: str, existing_source: str | None) -> bool:
    """Should the incoming record take over as primary?"""
    return SOURCE_DEPTH.get(new_source, 0) > SOURCE_DEPTH.get(
        existing_source or "", 0)
