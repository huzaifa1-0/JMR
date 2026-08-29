"""Frontend/backend contract test.

The frontend is plain JS with no type checking, so a renamed response key fails
silently as "undefined" in the UI rather than loudly in a test. This suite
asserts that every endpoint app.js calls returns the exact keys app.js reads.

Run with:  pytest tests/test_contract.py -q
"""
from __future__ import annotations

import os
import tempfile

import pytest

os.environ["JRP_DATA_DIR"] = tempfile.mkdtemp(prefix="jrp-contract-")
os.environ["JRP_OPEN_BROWSER"] = "false"

from fastapi.testclient import TestClient                            # noqa: E402

from backend.app.db import init_db                                   # noqa: E402
from backend.app.main import app                                     # noqa: E402


@pytest.fixture(scope="module")
def client():
    init_db()
    with TestClient(app) as c:
        yield c


def assert_keys(payload: dict, keys: set, where: str):
    missing = keys - set(payload)
    assert not missing, f"{where} is missing keys the frontend reads: {missing}"


class TestDashboardContract:
    def test_stats_overview(self, client):
        data = client.get("/api/stats/overview").json()
        assert_keys(data, {"jobs_active", "companies", "duplicates_linked",
                           "jobs_with_salary", "sources", "last_collection"},
                    "/stats/overview")

    def test_history(self, client):
        rows = client.get("/api/history?limit=8").json()
        assert isinstance(rows, list)
        if rows:
            assert_keys(rows[0], {"name", "run_type", "results_total",
                                  "results_new", "started_at", "status"},
                        "/history")

    def test_hiring_companies(self, client):
        rows = client.get("/api/analytics/hiring-companies?limit=8").json()
        assert isinstance(rows, list)
        for r in rows:
            assert_keys(r, {"name", "jobs_posted"}, "/analytics/hiring-companies")


class TestSourcesContract:
    def test_sources_shape(self, client):
        rows = client.get("/api/sources").json()
        assert rows, "sources must be seeded on first run"
        assert_keys(rows[0], {"key", "display_name", "kind", "enabled",
                              "requires_credentials", "configured", "health",
                              "monthly_quota", "calls_used", "remaining",
                              "ats_boards_known"}, "/sources")

    def test_toggle_returns_state(self, client):
        data = client.post("/api/sources/remotive/toggle?enabled=false").json()
        assert data["enabled"] is False
        client.post("/api/sources/remotive/toggle?enabled=true")

    def test_unknown_source_404(self, client):
        res = client.post("/api/sources/nonexistent/toggle?enabled=true")
        assert res.status_code == 404


class TestSearchContract:
    def test_search_response(self, client):
        data = client.post("/api/search", json={}).json()
        assert_keys(data, {"total", "page", "page_size", "pages", "results",
                           "facets", "query_echo"}, "/search")

    def test_facet_shape(self, client):
        facets = client.post("/api/search", json={}).json()["facets"]
        assert_keys(facets, {"company", "state", "city", "work_arrangement",
                             "employment_type", "experience_level", "skills"},
                    "search facets")
        for key, items in facets.items():
            for item in items:
                assert_keys(item, {"value", "count"}, f"facet '{key}'")

    def test_boolean_validation(self, client):
        ok = client.get("/api/search/validate-boolean",
                        params={"expr": "a AND b"}).json()
        assert_keys(ok, {"valid", "compiled", "terms"}, "validate-boolean (ok)")
        bad = client.get("/api/search/validate-boolean",
                         params={"expr": "NOT x"}).json()
        assert_keys(bad, {"valid", "message", "hint"}, "validate-boolean (bad)")


class TestAnalyticsContract:
    def test_overview_keys(self, client):
        data = client.post("/api/analytics/overview", json={}).json()
        assert_keys(data, {"total_jobs", "by_company", "by_state", "by_city",
                           "by_title", "by_employment_type",
                           "by_experience_level", "by_work_arrangement",
                           "by_salary_band", "timeline", "top_skills",
                           "salary_summary", "coverage_note"},
                    "/analytics/overview")

    def test_grouped_rows_use_label_and_count(self, client):
        data = client.post("/api/analytics/overview", json={}).json()
        for key in ("by_company", "by_state", "by_salary_band",
                    "by_work_arrangement"):
            for row in data[key]:
                assert_keys(row, {"label", "count"}, f"analytics '{key}'")

    def test_timeline_uses_date(self, client):
        for row in client.post("/api/analytics/overview", json={}).json()["timeline"]:
            assert_keys(row, {"date", "count"}, "analytics timeline")

    def test_salary_summary(self, client):
        summary = client.post("/api/analytics/overview",
                              json={}).json()["salary_summary"]
        assert_keys(summary, {"avg_annualized", "min", "max"}, "salary_summary")

    def test_skills_keys(self, client):
        data = client.post("/api/analytics/skills", json={}).json()
        assert_keys(data, {"total_jobs_in_scope", "skills"}, "/analytics/skills")
        for row in data["skills"]:
            assert_keys(row, {"skill", "category", "jobs", "required_in",
                              "preferred_in", "share"}, "skills row")


class TestCompaniesContract:
    def test_list(self, client):
        data = client.get("/api/companies?limit=10").json()
        assert_keys(data, {"total", "results"}, "/companies")
        if data["results"]:
            assert_keys(data["results"][0],
                        {"id", "name", "industry", "ats_platform",
                         "jobs_discovered", "hiring_frequency_30d",
                         "opportunity_score"}, "/companies row")

    def test_detail(self, client):
        rows = client.get("/api/companies?limit=1").json()["results"]
        if not rows:
            pytest.skip("no companies seeded")
        detail = client.get(f"/api/companies/{rows[0]['id']}").json()
        assert_keys(detail, {"id", "name", "industry", "domain", "ats_platform",
                             "jobs_total", "jobs_remote", "top_skills",
                             "salary", "jobs"}, "/companies/{id}")
        for row in detail["top_skills"]:
            assert_keys(row, {"value", "count"}, "company top_skills")


class TestResearchContract:
    def test_status_summary_is_status_to_count(self, client):
        data = client.get("/api/research/summary/status").json()
        assert "new" in data
        assert all(isinstance(v, int) for v in data.values()), \
            "frontend renders these directly as counts"

    def test_custom_fields_list(self, client):
        rows = client.get("/api/custom-fields").json()
        assert isinstance(rows, list)

    def test_custom_field_lifecycle(self, client):
        created = client.post("/api/custom-fields", json={
            "key": "contract_shape", "label": "Contract Shape",
            "entity": "job", "field_type": "dropdown",
            "options": ["Staff Aug", "Managed", "Project"],
        }).json()
        assert_keys(created, {"id", "key", "label", "field_type", "options",
                              "is_filterable"}, "POST /custom-fields")
        assert created["options"] == ["Staff Aug", "Managed", "Project"]
        assert client.delete(
            f"/api/custom-fields/{created['id']}").status_code == 200

    def test_scoring_rules(self, client):
        rows = client.get("/api/scoring-rules").json()
        assert rows, "default scoring rules must be seeded"
        assert_keys(rows[0], {"id", "name", "entity", "is_active", "weight",
                              "points", "condition"}, "/scoring-rules")

    def test_scoring_rule_upsert_roundtrip(self, client):
        rule = client.get("/api/scoring-rules").json()[0]
        payload = dict(rule)
        payload["is_active"] = not rule["is_active"]
        assert client.post("/api/scoring-rules", json=payload).status_code == 200
        after = next(r for r in client.get("/api/scoring-rules").json()
                     if r["id"] == rule["id"])
        assert after["is_active"] != rule["is_active"]
        client.post("/api/scoring-rules", json=rule)

    def test_rescore(self, client):
        assert "rescored" in client.post("/api/scoring-rules/rescore").json()


class TestSettingsContract:
    def test_settings_keys(self, client):
        data = client.get("/api/settings").json()
        assert_keys(data, {"max_results_per_run", "rate_limit_per_minute",
                           "cache_ttl_minutes", "default_page_size",
                           "dupe_title_ratio", "dupe_description_similarity",
                           "adzuna_app_id", "usajobs_email"}, "/settings")

    def test_secrets_are_masked(self, client):
        """Keys must never be echoed back in plain text."""
        client.put("/api/settings",
                   json={"values": {"adzuna_app_key": "supersecret123"}})
        data = client.get("/api/settings").json()
        assert data.get("adzuna_app_key") != "supersecret123", \
            "secret values must be masked in GET /settings"

    def test_exports_history(self, client):
        rows = client.get("/api/exports").json()
        assert isinstance(rows, list)
        for r in rows:
            assert_keys(r, {"format", "rows", "filename", "created_at",
                            "exists"}, "/exports")


class TestMaintenanceContract:
    def test_refresh_companies(self, client):
        data = client.post("/api/maintenance/refresh-companies").json()
        assert "companies_updated" in data

    def test_collect_status(self, client):
        data = client.get("/api/collect/status").json()
        assert "running" in data


class TestStaticAssets:
    @pytest.mark.parametrize("path", ["/", "/static/app.js", "/static/styles.css"])
    def test_assets_served(self, client, path):
        res = client.get(path)
        assert res.status_code == 200, f"{path} did not load"

    def test_app_js_has_no_browser_storage(self, client):
        """Browser storage is unsupported in this environment by policy."""
        js = client.get("/static/app.js").text
        for banned in ("localStorage", "sessionStorage", "document.cookie"):
            assert banned not in js, f"{banned} must not be used"
