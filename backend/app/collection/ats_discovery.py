"""ATS discovery -- the loop that makes free-tier collection compound.

An aggregator call is metered.  An ATS board call is not.  So we spend a metered
call once to *find* a company, read its ATS board token out of the apply URL,
and from then on monitor that employer indefinitely for free -- with full
descriptions instead of truncated snippets.

This is pure URL parsing of data the aggregator already gave us.  Nothing is
fetched, probed, or guessed at here.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

log = logging.getLogger(__name__)

# host pattern -> (platform, regex over the path capturing the board token)
PATTERNS: list[tuple[re.Pattern, str, re.Pattern]] = [
    (re.compile(r"(^|\.)(boards|job-boards)\.greenhouse\.io$"), "greenhouse",
     re.compile(r"^/(?:embed/job_board\?for=)?([A-Za-z0-9_-]+)")),
    (re.compile(r"(^|\.)greenhouse\.io$"), "greenhouse",
     re.compile(r"^/([A-Za-z0-9_-]+)")),
    (re.compile(r"(^|\.)lever\.co$"), "lever",
     re.compile(r"^/([A-Za-z0-9_-]+)")),
    (re.compile(r"(^|\.)ashbyhq\.com$"), "ashby",
     re.compile(r"^/([A-Za-z0-9_.-]+)")),
    (re.compile(r"(^|\.)workable\.com$"), "workable",
     re.compile(r"^/(?:j/)?([A-Za-z0-9_-]+)")),
    (re.compile(r"(^|\.)recruitee\.com$"), "recruitee",
     re.compile(r"^/?")),
    (re.compile(r"(^|\.)smartrecruiters\.com$"), "smartrecruiters",
     re.compile(r"^/([A-Za-z0-9_-]+)")),
]

# Paths that look like tokens but are not
RESERVED = {"embed", "jobs", "job", "api", "v1", "search", "companies",
            "boards", "static", "assets", "j", "o"}


def detect_ats(url: str | None) -> tuple[str, str] | None:
    """Return (platform, board_token) or None."""
    if not url:
        return None
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    host = (parsed.netloc or "").lower().split(":")[0]
    if not host:
        return None

    for host_re, platform, path_re in PATTERNS:
        if not host_re.search(host):
            continue

        # recruitee and some others put the token in the subdomain
        if platform == "recruitee":
            sub = host.split(".")[0]
            if sub and sub not in RESERVED:
                return platform, sub
            continue

        # greenhouse embed form: /embed/job_board?for=token
        if "for=" in (parsed.query or ""):
            m = re.search(r"for=([A-Za-z0-9_-]+)", parsed.query)
            if m:
                return platform, m.group(1)

        m = path_re.match(parsed.path or "")
        if m and m.lastindex:
            token = m.group(1)
            if token and token.lower() not in RESERVED:
                return platform, token

        # ashby and workable sometimes use company.subdomain form
        sub = host.split(".")[0]
        if sub and sub not in RESERVED and sub not in (
                "boards", "api", "apply", "jobs", "www"):
            return platform, sub
    return None


def discover_from_postings(postings) -> dict[str, tuple[str, str]]:
    """company_name -> (platform, token), from a batch of RawPostings."""
    found: dict[str, tuple[str, str]] = {}
    for posting in postings:
        if not posting.company_name:
            continue
        if posting.company_name in found:
            continue
        for url in (posting.apply_url, posting.job_url):
            hit = detect_ats(url)
            if hit:
                found[posting.company_name] = hit
                log.debug("Discovered %s board '%s' for %s", hit[0], hit[1],
                          posting.company_name)
                break
    return found
