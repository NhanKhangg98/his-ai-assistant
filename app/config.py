"""Application configuration loaded from environment variables."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Centralized configuration for HIS Knowledge Base and 7-Step Workflow Assistant."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Database
    database_url: str = Field(
        default="postgresql+psycopg://his_user:his_pass@localhost:5432/his_db",
        alias="DATABASE_URL",
    )

    # HIS Staging Repository
    his_repo_path: str = Field(
        default="./sample_data/his_staging",
        alias="HIS_REPO_PATH",
    )
    his_staging_branch: str = Field(
        default="staging",
        alias="HIS_STAGING_BRANCH",
    )
    eval_dataset_path: str = Field(
        default="./sample_data/eval_questions.json",
        alias="EVAL_DATASET_PATH",
    )

    # Adapter selection via environment variables
    llm_primary_provider: str = Field(default="primary-mock", alias="LLM_PRIMARY_PROVIDER")
    llm_fallback_provider: str = Field(default="fallback-mock", alias="LLM_FALLBACK_PROVIDER")
    llm_simulate_primary_failure: bool = Field(
        default=False,
        alias="LLM_SIMULATE_PRIMARY_FAILURE",
    )
    jira_provider: str = Field(default="mock", alias="JIRA_PROVIDER")
    discord_provider: str = Field(default="mock", alias="DISCORD_PROVIDER")
    git_provider: str = Field(default="local-repo", alias="GIT_PROVIDER")

    # Monthly LLM Quota Governance (Tokens & Cost USD)
    monthly_user_token_quota: int = Field(default=50_000, alias="MONTHLY_USER_TOKEN_QUOTA")
    monthly_user_cost_usd_quota: float = Field(default=5.00, alias="MONTHLY_USER_COST_USD_QUOTA")
    monthly_role_token_quota: int = Field(default=150_000, alias="MONTHLY_ROLE_TOKEN_QUOTA")
    monthly_role_cost_usd_quota: float = Field(default=15.00, alias="MONTHLY_ROLE_COST_USD_QUOTA")

    # Durable Asyncio Worker Settings
    worker_poll_interval_seconds: float = Field(
        default=0.15,
        alias="WORKER_POLL_INTERVAL_SECONDS",
    )
    worker_lease_seconds: int = Field(default=30, alias="WORKER_LEASE_SECONDS")
    worker_max_attempts: int = Field(default=3, alias="WORKER_MAX_ATTEMPTS")
    enable_background_worker: bool = Field(default=True, alias="ENABLE_BACKGROUND_WORKER")

    @property
    def resolved_repo_path(self) -> Path:
        return Path(self.his_repo_path).resolve()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached Settings instance."""
    return Settings()
