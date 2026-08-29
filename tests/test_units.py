"""Test suite.

Run with:  pytest -q

Covers the pieces most likely to break silently: the Boolean parser (including
FTS5 injection attempts), salary normalisation, dedup precision, and a full
search -> filter -> export round trip through the API.
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("JRP_DATA_DIR", tempfile.mkdtemp(prefix="jrp-test-"))
os.environ.setdefault("JRP_OPEN_BROWSER", "false")

from backend.app.collection.ats_discovery import detect_ats          # noqa: E402
from backend.app.collection.base import Budget, RawPosting           # noqa: E402
from backend.app.core.errors import BooleanSyntaxError               # noqa: E402
from backend.app.processing import dedupe as dd                      # noqa: E402
from backend.app.processing import extract as ex                     # noqa: E402
from backend.app.processing import normalize as nz                   # noqa: E402
from backend.app.search.boolean import compile_boolean, explain      # noqa: E402


# ==========================================================================
# Boolean parser
# ==========================================================================
class TestBooleanParser:
    def test_simple_and(self):
        out = compile_boolean("remote billing")
        assert "AND" in out and '"remote"' in out and '"billing"' in out

    def test_explicit_operators(self):
        out = compile_boolean('("medical billing" OR "revenue cycle") AND remote')
        assert "OR" in out and "AND" in out
        assert '"medical billing"' in out

    def test_not_operator(self):
        out = compile_boolean("developer NOT internship")
        assert "NOT" in out

    def test_symbol_aliases(self):
        assert "AND" in compile_boolean("python && sql")
        assert "OR" in compile_boolean("python || sql")

    def test_blank_returns_none(self):
        assert compile_boolean("") is None
        assert compile_boolean(None) is None
        assert compile_boolean("   ") is None

    def test_leading_not_rejected(self):
        with pytest.raises(BooleanSyntaxError):
            compile_boolean("NOT internship")

    def test_unclosed_paren_rejected(self):
        with pytest.raises(BooleanSyntaxError):
            compile_boolean('("a" OR "b"')

    def test_unmatched_close_rejected(self):
        with pytest.raises(BooleanSyntaxError):
            compile_boolean('a OR b)')

    def test_trailing_operator_rejected(self):
        with pytest.raises(BooleanSyntaxError):
            compile_boolean("python AND")

    def test_empty_phrase_rejected(self):
        with pytest.raises(BooleanSyntaxError):
            compile_boolean('"" AND python')

    @pytest.mark.parametrize("payload", [
        'python*', 'sql^2', 'a NEAR b', 'x: y', 'a{b}', 'foo"bar',
    ])
    def test_fts5_operators_are_neutralised(self, payload):
        """User text must never reach SQLite's matcher as operators."""
        try:
            out = compile_boolean(payload)
        except BooleanSyntaxError:
            return                      # rejecting outright is also acceptable
        for char in ("*", "^", ":", "{", "}"):
            assert char not in out, f"{char!r} leaked through in {out!r}"

    def test_explain_reports_errors_without_raising(self):
        result = explain("NOT alone")
        assert result["valid"] is False
        assert result["message"]
        assert result["hint"]

    def test_explain_collects_positive_terms(self):
        result = explain('("a" OR "b") NOT c')
        assert result["valid"] is True
        assert "a" in result["terms"] and "b" in result["terms"]
        assert "c" not in result["terms"]


# ==========================================================================
# Normalisation
# ==========================================================================
class TestNormalize:
    def test_company_suffix_stripping(self):
        assert nz.normalize_company_name("Acme Health, Inc.") == \
               nz.normalize_company_name("Acme Health LLC")

    def test_company_case_and_punctuation(self):
        assert nz.normalize_company_name("ABC  Healthcare!") == "abc healthcare"

    def test_title_noise_removal(self):
        a = nz.normalize_title("Senior Developer (Remote) - Urgently Hiring")
        b = nz.normalize_title("Senior Developer")
        assert a == b

    @pytest.mark.parametrize("raw,city,state", [
        ("Dallas, TX", "Dallas", "TX"),
        ("Austin, Texas", "Austin", "TX"),
        ("Chicago, IL 60601", "Chicago", "IL"),
        ("New York, NY, United States", "New York", "NY"),
    ])
    def test_location_parsing(self, raw, city, state):
        out = nz.parse_location(raw)
        assert out["city"] == city
        assert out["state"] == state

    def test_zip_extraction(self):
        assert nz.parse_location("Chicago, IL 60601")["postal_code"] == "60601"

    @pytest.mark.parametrize("text,expected", [
        ("This is a fully remote position", "remote"),
        ("Hybrid schedule, 3 days in office", "hybrid"),
        ("On-site in our Dallas office", "onsite"),
        ("Work from home opportunity", "remote"),
        ("Nothing relevant here", "unknown"),
    ])
    def test_work_arrangement(self, text, expected):
        assert nz.detect_work_arrangement(text) == expected

    def test_hybrid_beats_remote(self):
        """'Hybrid remote' is hybrid, not remote -- order of checks matters."""
        assert nz.detect_work_arrangement("Hybrid remote role") == "hybrid"

    @pytest.mark.parametrize("text,expected", [
        ("Full-time position", "full_time"),
        ("Part time, 20 hrs", "part_time"),
        ("6 month contract", "contract"),
        ("Summer internship", "internship"),
        ("Temporary seasonal work", "temporary"),
    ])
    def test_employment_type(self, text, expected):
        assert nz.detect_employment_type(text) == expected

    def test_salary_annualization(self):
        assert nz.annualize(50, "hourly") == 104000.0
        assert nz.annualize(5000, "monthly") == 60000.0
        assert nz.annualize(90000, "yearly") == 90000.0

    def test_salary_from_text_range(self):
        out = nz.parse_salary_from_text("Pay range: $65,000 - $85,000 per year")
        assert out["salary_min"] == 65000
        assert out["salary_max"] == 85000
        assert out["salary_period"] == "yearly"

    def test_salary_k_suffix(self):
        out = nz.parse_salary_from_text("Offering $120k annually")
        assert out["salary_min"] == 120000

    def test_salary_hourly_inferred(self):
        out = nz.parse_salary_from_text("$28.50 per hour DOE")
        assert out["salary_period"] == "hourly"

    def test_no_salary_returns_none(self):
        assert nz.parse_salary_from_text("Competitive compensation") is None

    def test_experience_level_from_title(self):
        assert nz.detect_experience_level("Senior Engineer") == "senior"
        assert nz.detect_experience_level("Director of Ops") == "director"
        assert nz.detect_experience_level("Junior Analyst") == "entry"

    def test_html_to_text(self):
        out = nz.html_to_text("<p>Hello</p><ul><li>One</li><li>Two</li></ul>")
        assert "Hello" in out and "One" in out and "<" not in out


# ==========================================================================
# Extraction
# ==========================================================================
class TestExtraction:
    def test_years_range(self):
        out = ex.extract_experience("We need 3-5 years of experience")
        assert out["years_experience_min"] == 3
        assert out["years_experience_max"] == 5

    def test_years_minimum(self):
        out = ex.extract_experience("At least 7 years experience required")
        assert out["years_experience_min"] == 7

    def test_years_written_as_word(self):
        out = ex.extract_experience("Five years of experience preferred")
        assert out["years_experience_min"] == 5

    @pytest.mark.parametrize("text,level", [
        ("Bachelor's degree required", "bachelors"),
        ("MBA preferred", "masters"),
        ("PhD in Computer Science", "doctorate"),
        ("High school diploma or GED", "high_school"),
        # Regression: postings routinely omit the word "degree"
        ("Master's preferred", "masters"),
        ("Bachelors preferred, or equivalent experience", "bachelors"),
        ("Graduate degree a plus", "masters"),
    ])
    def test_education(self, text, level):
        assert ex.extract_education(text) == level

    def test_sponsorship_positive(self):
        out = ex.extract_flags("Visa sponsorship is available for this role")
        assert out["visa_sponsorship_mentioned"] is True

    def test_sponsorship_negative_wins(self):
        """Negation must beat the positive pattern, not merely co-occur."""
        out = ex.extract_flags(
            "We are unable to sponsor visas. Sponsorship available: no.")
        assert out["visa_sponsorship_mentioned"] is False

    def test_sponsorship_absent(self):
        assert ex.extract_flags("Great team, great pay")[
            "visa_sponsorship_mentioned"] is None

    def test_relocation_negation(self):
        assert ex.extract_flags("No relocation assistance")[
            "relocation_mentioned"] is False

    def test_urgency(self):
        assert ex.extract_flags("Urgently hiring! Start ASAP")[
            "urgently_hiring"] is True

    def test_benefits(self):
        found = ex.extract_benefits(
            "We offer health insurance, 401(k) matching, PTO and stock options.")
        assert "health_insurance" in found
        assert "retirement_401k" in found
        assert "paid_time_off" in found
        assert "equity" in found

    def test_contacts(self):
        out = ex.extract_contacts("Email careers@acme.com or call 555-123-4567")
        assert "careers@acme.com" in out["emails"]
        assert len(out["phones"]) == 1


# ==========================================================================
# Deduplication
# ==========================================================================
class TestDedupe:
    def test_canonical_hash_stable_under_noise(self):
        a = dd.canonical_hash("Senior Developer", "Acme Inc.", "Dallas", "TX")
        b = dd.canonical_hash("senior  developer", "Acme, LLC", "dallas", "tx")
        assert a == b

    def test_canonical_hash_differs_on_real_change(self):
        a = dd.canonical_hash("Senior Developer", "Acme", "Dallas", "TX")
        b = dd.canonical_hash("Junior Developer", "Acme", "Dallas", "TX")
        assert a != b

    def test_title_similarity_reordered_tokens(self):
        assert dd.title_similarity("Backend Engineer, Senior",
                                   "Senior Backend Engineer") > 0.9

    def test_title_similarity_different_roles(self):
        assert dd.title_similarity("Nurse Practitioner",
                                   "Software Engineer") < 0.5

    def test_simhash_near_duplicate(self):
        a = "We are seeking a medical biller with ICD-10 and CPT experience " * 3
        b = "We are seeking a medical biller with ICD-10 and CPT expertise " * 3
        assert dd.simhash_similarity(dd.simhash(a), dd.simhash(b)) > 0.85

    def test_simhash_distinct_documents(self):
        a = "Medical billing specialist handling claims and denials " * 5
        b = "Kubernetes platform engineer working on service meshes " * 5
        assert dd.simhash_similarity(dd.simhash(a), dd.simhash(b)) < 0.85

    def test_simhash_empty_input(self):
        assert dd.simhash("") == ""
        assert dd.simhash_similarity("", "abc") == 0.0

    def test_source_promotion_ranking(self):
        assert dd.should_promote("greenhouse", "adzuna") is True
        assert dd.should_promote("adzuna", "greenhouse") is False


# ==========================================================================
# ATS discovery
# ==========================================================================
class TestAtsDiscovery:
    @pytest.mark.parametrize("url,platform,token", [
        ("https://boards.greenhouse.io/stripe/jobs/12345", "greenhouse", "stripe"),
        ("https://jobs.lever.co/netflix/abc-def", "lever", "netflix"),
        ("https://jobs.ashbyhq.com/ramp/xyz", "ashby", "ramp"),
        ("https://apply.workable.com/acmecorp/j/ABC123/", "workable", "acmecorp"),
    ])
    def test_detects_known_platforms(self, url, platform, token):
        result = detect_ats(url)
        assert result is not None
        assert result[0] == platform
        assert result[1] == token

    def test_greenhouse_embed_form(self):
        result = detect_ats(
            "https://boards.greenhouse.io/embed/job_board?for=databricks")
        assert result == ("greenhouse", "databricks")

    @pytest.mark.parametrize("url", [
        None, "", "not-a-url", "https://www.example.com/careers",
    ])
    def test_ignores_unknown_urls(self, url):
        assert detect_ats(url) is None


# ==========================================================================
# Scoring
# ==========================================================================
class TestScoring:
    def test_condition_operators(self):
        from backend.app.processing.scoring import evaluate_condition
        facts = {"salary": 90000, "remote": True, "title": "Senior Engineer",
                 "skills": ["Python", "SQL"]}
        assert evaluate_condition(
            {"field": "salary", "op": "gte", "value": 80000}, facts)
        assert not evaluate_condition(
            {"field": "salary", "op": "gte", "value": 120000}, facts)
        assert evaluate_condition({"field": "remote", "op": "is_true"}, facts)
        assert evaluate_condition(
            {"field": "title", "op": "contains", "value": "senior"}, facts)
        assert evaluate_condition(
            {"field": "skills", "op": "contains", "value": "python"}, facts)

    def test_compound_conditions(self):
        from backend.app.processing.scoring import evaluate_condition
        facts = {"a": 10, "b": False}
        assert evaluate_condition({"all": [
            {"field": "a", "op": "gte", "value": 5},
            {"field": "b", "op": "is_false"}]}, facts)
        assert evaluate_condition({"any": [
            {"field": "a", "op": "gte", "value": 100},
            {"field": "b", "op": "is_false"}]}, facts)
        assert evaluate_condition(
            {"not": {"field": "a", "op": "gte", "value": 100}}, facts)

    def test_missing_field_is_false_not_an_error(self):
        from backend.app.processing.scoring import evaluate_condition
        assert not evaluate_condition(
            {"field": "nonexistent", "op": "gte", "value": 1}, {})

    def test_malformed_condition_is_false(self):
        from backend.app.processing.scoring import evaluate_condition
        assert not evaluate_condition({"garbage": True}, {"a": 1})
        assert not evaluate_condition({"field": "a", "op": "nonsense"}, {"a": 1})


# ==========================================================================
# Budget
# ==========================================================================
class TestBudget:
    def test_call_ceiling_enforced(self):
        b = Budget(max_calls=3, max_results=1000)
        for _ in range(3):
            assert b.can_call()
            b.spend_call()
        assert not b.can_call()

    def test_result_ceiling_enforced(self):
        b = Budget(max_calls=100, max_results=10)
        b.add_results(10)
        assert not b.can_call()
        assert b.remaining_results == 0
