"""End-to-end integration test.

Ingests synthetic postings directly through the real ingest path (no network),
then drives the actual HTTP API: search, Boolean filters, dedup linking,
research edits, custom fields, analytics and export.

Run with:  pytest tests/test_e2e.py -q
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

os.environ["JRP_DATA_DIR"] = tempfile.mkdtemp(prefix="jrp-e2e-")
os.environ["JRP_OPEN_BROWSER"] = "false"

from fastapi.testclient import TestClient                            # noqa: E402

from backend.app.collection.base import RawPosting                   # noqa: E402
from backend.app.collection.orchestrator import ingest_posting       # noqa: E402
from backend.app.db import init_db, session_scope                    # noqa: E402
from backend.app.main import app                                     # noqa: E402
from backend.app.models import ScoringRule, Source                   # noqa: E402

NOW = datetime.now(timezone.utc)


def posting(**kw):
    base = dict(
        source_key="remotive", source_job_id="1", title="Medical Biller",
        company_name="Acme Health", country="US",
        date_posted=NOW - timedelta(days=2),
    )
    base.update(kw)
    return RawPosting(**base)


FIXTURES = [
    posting(
        source_job_id="100", title="Senior Medical Biller",
        company_name="Acme Health Inc.",
        location_raw="Dallas, TX",
        description_html="<p>Seeking a Senior Medical Biller with ICD-10 and "
                         "CPT experience. Requires 5 years experience and a "
                         "Bachelor's degree. Familiarity with Epic required. "
                         "Denial management and accounts receivable follow-up. "
                         "$70,000 - $90,000 per year. We offer health insurance, "
                         "401(k) matching and PTO. Visa sponsorship is available. "
                         "This is a fully remote position. Urgently hiring!</p>",
        job_url="https://boards.greenhouse.io/acmehealth/jobs/100",
        apply_url="https://boards.greenhouse.io/acmehealth/jobs/100",
    ),
    posting(
        source_job_id="101", title="Revenue Cycle Analyst",
        company_name="Beta Medical Group",
        location_raw="Austin, Texas",
        description_html="<p>Revenue cycle analyst needed. SQL and Excel "
                         "required. 3-5 years of experience. Hybrid schedule, "
                         "3 days in office. Salary $60,000 to $75,000 annually. "
                         "We are unable to sponsor visas.</p>",
        job_url="https://example.com/jobs/101",
    ),
    posting(
        source_job_id="102", title="Python Engineer",
        company_name="Gamma Software",
        location_raw="Remote",
        description_html="<p>Python engineer. Build APIs with FastAPI and "
                         "PostgreSQL. Docker and AWS. At least 7 years "
                         "experience. Master's degree preferred. "
                         "$140,000 per year. Equity and stock options. "
                         "Full-time. Relocation assistance offered.</p>",
        job_url="https://jobs.lever.co/gammasoftware/102",
        apply_url="https://jobs.lever.co/gammasoftware/102",
    ),
    posting(
        source_job_id="103", title="Junior Data Entry Clerk",
        company_name="Delta Services LLC",
        location_raw="Chicago, IL 60601",
        description_html="<p>Data entry clerk. High school diploma or GED. "
                         "Part time, 20 hrs. $18.00 per hour. On-site in our "
                         "Chicago office. No relocation assistance.</p>",
        job_url="https://example.com/jobs/103",
    ),
    # deliberate near-duplicate of 100, from a richer source
    posting(
        source_key="greenhouse", source_job_id="acmehealth:100",
        title="Senior Medical Biller",
        company_name="Acme Health LLC",
        location_raw="Dallas, TX",
        description_html="<p>Seeking a Senior Medical Biller with ICD-10 and "
                         "CPT expertise. Requires 5 years experience and a "
                         "Bachelor's degree. Familiarity with Epic required. "
                         "Denial management and accounts receivable followup. "
                         "$70,000 - $90,000 per year. We offer health insurance, "
                         "401(k) matching and PTO. Visa sponsorship is available. "
                         "This is a fully remote position. Urgently hiring!</p>",
        job_url="https://boards.greenhouse.io/acmehealth/jobs/100-dup",
    ),
]


@pytest.fixture(scope="module")
def client():
    init_db()
    with session_scope() as db:
        rules = db.query(ScoringRule).all()
        for p in FIXTURES:
            src = db.query(Source).filter(Source.key == p.source_key).first()
            ingest_posting(db, p, src, (0.92, 0.85), rules)
        db.commit()
    with TestClient(app) as c:
        yield c


def search(client, **kw):
    res = client.post("/api/search", json=kw)
    assert res.status_code == 200, res.text
    return res.json()


# ==========================================================================
class TestIngest:
    def test_corpus_populated(self, client):
        data = search(client)
        assert data["total"] == 4, "the near-duplicate should not count as primary"

    def test_duplicate_was_linked_not_dropped(self, client):
        data = search(client, include_duplicates=True)
        assert data["total"] == 5

    def test_richer_source_became_primary(self, client):
        """Greenhouse has full descriptions, so it should outrank Remotive."""
        data = search(client, job_title="Senior Medical Biller")
        assert data["total"] == 1
        assert data["results"][0]["source"] == "greenhouse"
        assert data["results"][0]["duplicate_count"] >= 1

    def test_extraction_populated_fields(self, client):
        data = search(client, job_title="Senior Medical Biller")
        job = data["results"][0]
        assert job["work_arrangement"] == "remote"
        assert job["salary_annualized_min"] == 70000
        assert job["experience_level"] == "senior"
        assert job["education_level"] == "bachelors"
        assert job["visa_sponsorship_mentioned"] is True
        assert job["urgently_hiring"] is True
        assert job["opportunity_score"] is not None
        skills = {s["name"] for s in job["skills"]}
        assert "ICD-10" in skills and "Epic" in skills

    def test_hourly_salary_annualized(self, client):
        data = search(client, job_title="Data Entry")
        assert data["results"][0]["salary_annualized_min"] == pytest.approx(37440)

    def test_negated_sponsorship_recorded(self, client):
        data = search(client, job_title="Revenue Cycle Analyst")
        assert data["results"][0]["visa_sponsorship_mentioned"] is False

    def test_location_parsed(self, client):
        data = search(client, city="Austin")
        assert data["total"] == 1
        assert data["results"][0]["state"] == "TX"


class TestArrangementPrecedence:
    """Regression: a source's remote flag must not mask explicit hybrid text.

    Remotive marks every posting remote, but boards that only list
    remote-friendly roles still carry hybrid ones. The description is the more
    specific evidence, so 'hybrid' in the text wins over the source-level flag.
    """

    def test_hybrid_text_beats_source_remote_flag(self, client):
        rows = search(client, job_title="Revenue Cycle Analyst")["results"]
        assert rows[0]["work_arrangement"] == "hybrid"

    def test_remote_flag_still_applies_without_contrary_text(self, client):
        rows = search(client, job_title="Senior Medical Biller")["results"]
        assert rows[0]["work_arrangement"] == "remote"


class TestFilters:
    def test_salary_floor(self, client):
        data = search(client, salary_min=100000)
        assert data["total"] == 1
        assert data["results"][0]["title"] == "Python Engineer"

    def test_work_arrangement(self, client):
        assert search(client, work_arrangements=["remote"])["total"] == 2
        assert search(client, work_arrangements=["hybrid"])["total"] == 1
        assert search(client, work_arrangements=["onsite"])["total"] == 1

    def test_experience_level(self, client):
        assert search(client, experience_levels=["entry"])["total"] == 1

    def test_required_skills_uses_and_semantics(self, client):
        assert search(client, required_skills=["Python", "Docker"])["total"] == 1
        assert search(client,
                      required_skills=["Python", "Medical Billing"])["total"] == 0

    def test_exclude_skills(self, client):
        assert search(client, exclude_skills=["Python"])["total"] == 3

    def test_title_not_contains(self, client):
        assert search(client, title_not_contains="Junior")["total"] == 3

    def test_description_contains(self, client):
        assert search(client, description_contains="denial")["total"] == 1

    def test_sponsorship_flag(self, client):
        assert search(client, visa_sponsorship=True)["total"] == 1

    def test_years_experience(self, client):
        assert search(client, years_experience_min=7)["total"] == 1

    def test_education(self, client):
        assert search(client, education_levels=["high_school"])["total"] == 1

    def test_combined_filters(self, client):
        data = search(client, work_arrangements=["remote"], salary_min=60000,
                      visa_sponsorship=True)
        assert data["total"] == 1


class TestBooleanSearch:
    def test_or_group(self, client):
        data = search(client, boolean='"medical biller" OR "revenue cycle"')
        assert data["total"] >= 2

    def test_and_narrows(self, client):
        assert search(client, boolean='biller AND epic')["total"] == 1

    def test_not_excludes(self, client):
        with_all = search(client, boolean='engineer OR biller')["total"]
        without = search(client, boolean='(engineer OR biller) NOT python')["total"]
        assert without < with_all

    def test_invalid_expression_returns_422(self, client):
        res = client.post("/api/search", json={"boolean": "NOT alone"})
        assert res.status_code == 422
        body = res.json()
        assert "error" in body and body["error"]["hint"]

    def test_validation_endpoint(self, client):
        ok = client.get("/api/search/validate-boolean",
                        params={"expr": '("a" OR "b") AND c'}).json()
        assert ok["valid"] is True
        bad = client.get("/api/search/validate-boolean",
                         params={"expr": '("a" OR'}).json()
        assert bad["valid"] is False


class TestSorting:
    def test_salary_desc(self, client):
        rows = search(client, sort="salary", sort_dir="desc",
                      has_salary=True)["results"]
        salaries = [r["salary_annualized_min"] for r in rows]
        assert salaries == sorted(salaries, reverse=True)

    def test_title_asc(self, client):
        rows = search(client, sort="title", sort_dir="asc")["results"]
        titles = [r["title"] for r in rows]
        assert titles == sorted(titles)

    def test_invalid_sort_rejected(self, client):
        res = client.post("/api/search", json={"sort": "nonsense"})
        assert res.status_code == 422


class TestJobDetail:
    def test_detail_shape(self, client):
        job_id = search(client, job_title="Python Engineer")["results"][0]["id"]
        detail = client.get(f"/api/jobs/{job_id}").json()
        assert detail["description_text"]
        assert "benefits" in detail and "equity" in detail["benefits"]
        assert detail["is_primary"] is True

    def test_duplicates_listed(self, client):
        job_id = search(client,
                        job_title="Senior Medical Biller")["results"][0]["id"]
        detail = client.get(f"/api/jobs/{job_id}").json()
        assert len(detail["duplicates"]) >= 1

    def test_missing_job_returns_404_with_hint(self, client):
        res = client.get("/api/jobs/999999")
        assert res.status_code == 404
        assert res.json()["error"]["hint"]


class TestResearch:
    def test_update_and_filter(self, client):
        job_id = search(client, job_title="Python Engineer")["results"][0]["id"]
        res = client.put(f"/api/research/{job_id}", json={
            "status": "relevant", "priority": "high", "lead_quality": 5,
            "notes": "Strong outsourcing fit.", "tags": ["priority", "q3"],
        })
        assert res.status_code == 200
        found = search(client, research_status=["relevant"])
        assert found["total"] == 1
        assert found["results"][0]["research"]["priority"] == "high"

    def test_tag_filter(self, client):
        assert search(client, tags=["priority"])["total"] == 1

    def test_status_summary(self, client):
        summary = client.get("/api/research/summary/status").json()
        assert summary["relevant"] == 1
        assert "new" in summary


class TestCustomFields:
    def test_create_set_and_filter(self, client):
        created = client.post("/api/custom-fields", json={
            "key": "outsourcing_fit", "label": "Outsourcing Fit",
            "entity": "job", "field_type": "number",
        })
        assert created.status_code == 200, created.text

        job_id = search(client, job_title="Python Engineer")["results"][0]["id"]
        res = client.put("/api/custom-fields/outsourcing_fit/value", json={
            "entity": "job", "entity_id": job_id, "value": 87,
        })
        assert res.status_code == 200

        found = search(client, custom_fields=[
            {"key": "outsourcing_fit", "op": "gte", "value": 80}])
        assert found["total"] == 1

        none = search(client, custom_fields=[
            {"key": "outsourcing_fit", "op": "gte", "value": 95}])
        assert none["total"] == 0

    def test_unknown_field_rejected(self, client):
        res = client.post("/api/search", json={
            "custom_fields": [{"key": "does_not_exist", "op": "eq", "value": 1}]})
        assert res.status_code == 422

    def test_invalid_key_rejected(self, client):
        res = client.post("/api/custom-fields", json={
            "key": "Bad Key!", "label": "x", "field_type": "text"})
        assert res.status_code == 422


class TestAnalytics:
    def test_overview(self, client):
        data = client.post("/api/analytics/overview", json={}).json()
        assert data["total_jobs"] == 4
        assert len(data["by_company"]) == 4
        assert data["by_salary_band"]
        assert data["coverage_note"], "analytics must ship its own caveat"

    def test_skills_ranking(self, client):
        data = client.post("/api/analytics/skills", json={}).json()
        skills = data["skills"]
        assert len(skills) > 0
        assert all({"skill", "jobs", "share"} <= set(s) for s in skills)
        counts = [s["jobs"] for s in skills]
        assert counts == sorted(counts, reverse=True)

    def test_analytics_respects_filters(self, client):
        filtered = client.post("/api/analytics/overview",
                               json={"work_arrangements": ["remote"]}).json()
        assert filtered["total_jobs"] == 2


class TestCompanies:
    def test_seed_companies_present(self, client):
        """Seeded ATS boards ship with the app so collection is useful on day one."""
        data = client.get("/api/companies?limit=200").json()
        assert data["total"] > 20
        names = {r["name"] for r in data["results"]}
        assert "Stripe" in names

    def test_only_fixture_companies_have_jobs(self, client):
        data = client.get("/api/companies?min_jobs=1&limit=50").json()
        assert data["total"] == 4
        rows = data["results"]
        detail = client.get(f"/api/companies/{rows[0]['id']}").json()
        assert "jobs" in detail
        assert detail["jobs_total"] >= 1
        assert "top_skills" in detail and "salary" in detail

    def test_ats_token_discovered(self, client):
        rows = client.get("/api/companies?limit=50").json()["results"]
        platforms = {r.get("ats_platform") for r in rows}
        assert "greenhouse" in platforms or "lever" in platforms


class TestExport:
    @pytest.mark.parametrize("fmt", ["csv", "xlsx", "json"])
    def test_export_formats(self, client, fmt):
        res = client.post("/api/exports", json={
            "format": fmt, "query": {}, "include_description": False})
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["rows"] == 4
        assert body["filename"].endswith(fmt)
        assert body["download_url"].startswith("/api/exports/download/")

    def test_export_respects_filters(self, client):
        res = client.post("/api/exports", json={
            "format": "csv", "query": {"work_arrangements": ["remote"]}})
        assert res.json()["rows"] == 2

    def test_download_roundtrip(self, client):
        name = client.post("/api/exports",
                           json={"format": "csv", "query": {}}).json()["filename"]
        dl = client.get(f"/api/exports/download/{name}")
        assert dl.status_code == 200
        assert b"title" in dl.content.lower()

    def test_path_traversal_blocked(self, client):
        res = client.get("/api/exports/download/..%2F..%2Fetc%2Fpasswd")
        assert res.status_code in (400, 404, 422)


class TestSourcesAndSettings:
    def test_sources_listed(self, client):
        sources = client.get("/api/sources").json()
        keys = {s["key"] for s in sources}
        assert {"remotive", "adzuna", "greenhouse", "lever"} <= keys

    def test_credentialed_sources_start_unconfigured(self, client):
        sources = {s["key"]: s for s in client.get("/api/sources").json()}
        assert sources["adzuna"]["configured"] is False
        assert sources["greenhouse"]["configured"] is True

    def test_settings_roundtrip(self, client):
        client.put("/api/settings", json={"values": {"max_results_per_run": 250}})
        assert client.get("/api/settings").json()["max_results_per_run"] == 250

    def test_toggle_source(self, client):
        client.post("/api/sources/remotive/toggle?enabled=false")
        sources = {s["key"]: s for s in client.get("/api/sources").json()}
        assert sources["remotive"]["enabled"] is False
        client.post("/api/sources/remotive/toggle?enabled=true")


class TestSavedSearches:
    def test_create_run_delete(self, client):
        created = client.post("/api/saved-searches", json={
            "name": "US Remote RCM",
            "query": {"work_arrangements": ["remote"], "salary_min": 50000},
        }).json()
        run = client.post(f"/api/saved-searches/{created['id']}/run").json()
        assert run["total"] >= 1
        assert client.delete(
            f"/api/saved-searches/{created['id']}").status_code == 200


class TestHistoryAndHealth:
    def test_health(self, client):
        assert client.get("/api/health").json()["status"] == "ok"

    def test_history_recorded(self, client):
        rows = client.get("/api/history?limit=5").json()
        assert len(rows) > 0

    def test_overview_stats(self, client):
        stats = client.get("/api/stats/overview").json()
        assert stats["jobs_active"] == 4
        assert stats["duplicates_linked"] == 1

    def test_frontend_served(self, client):
        res = client.get("/")
        assert res.status_code == 200
        assert b"Job Market Research" in res.content
