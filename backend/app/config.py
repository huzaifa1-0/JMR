"""Application configuration.

Every operational value lives here or in the `app_setting` table (editable from
the Settings UI at runtime).  Nothing that a researcher might reasonably want to
change is hard-coded elsewhere in the codebase.

Precedence:  DB setting  >  environment variable  >  default below.
"""
from __future__ import annotations

import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_data_dir() -> Path:
    """%LOCALAPPDATA%\\JobResearch on Windows, ~/.job-research elsewhere."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home())
        return Path(base) / "JobResearch"
    return Path.home() / ".job-research"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="JRP_", env_file=".env", extra="ignore"
    )

    # --- server -------------------------------------------------------
    host: str = "127.0.0.1"
    port: int = 8420
    open_browser: bool = True
    reload: bool = False

    # --- storage ------------------------------------------------------
    data_dir: Path = _default_data_dir()
    db_filename: str = "research.db"

    # --- collection defaults (overridable per-source in Settings UI) --
    default_country: str = "us"
    max_results_per_run: int = 500
    request_timeout_seconds: float = 25.0
    max_retries: int = 3
    rate_limit_per_minute: int = 20
    cache_ttl_minutes: int = 60
    user_agent: str = (
        "JobMarketResearchPlatform/1.0 (local research tool; "
        "contact: set JRP_USER_AGENT to include your email)"
    )

    # --- source credentials -------------------------------------------
    adzuna_app_id: str = ""
    adzuna_app_key: str = ""
    usajobs_api_key: str = ""
    usajobs_email: str = ""

    # --- search -------------------------------------------------------
    default_page_size: int = 50
    max_page_size: int = 500

    # --- dedupe thresholds --------------------------------------------
    dupe_title_ratio: float = 0.92
    dupe_description_similarity: float = 0.85

    # --- logging ------------------------------------------------------
    log_level: str = "INFO"
    log_max_bytes: int = 5_000_000
    log_backup_count: int = 5

    # --- derived paths -------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self.data_dir / self.db_filename

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.db_path}"

    @property
    def export_dir(self) -> Path:
        return self.data_dir / "exports"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def package_dir(self) -> Path:
        return Path(__file__).resolve().parent

    @property
    def frontend_dir(self) -> Path:
        return self.package_dir.parent.parent / "frontend"

    @property
    def taxonomy_dir(self) -> Path:
        return self.package_dir / "data" / "taxonomies"

    @property
    def seed_dir(self) -> Path:
        return self.package_dir / "data" / "seeds"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.export_dir, self.log_dir):
            d.mkdir(parents=True, exist_ok=True)


settings = Settings()
