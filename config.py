from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Weekly Report Agent"
    agent_enabled: bool = True
    timezone: str = "Asia/Kolkata"
    schedule_day: str = "thu"
    schedule_hour: int = 17
    schedule_minute: int = 0
    manager_email: str = ""
    sender_name: str = ""
    openai_api_key: str = ""
    openai_model: str = "gpt-5-mini"
    database_path: Path = Path("data/weekly_reports.db")
    gmail_credentials_file: Path = Path("credentials.json")
    gmail_token_file: Path = Path("token.json")
    gmail_max_retries: int = 3
    log_level: str = "INFO"

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", case_sensitive=False, extra="ignore"
    )

    @field_validator("schedule_day")
    @classmethod
    def valid_weekday(cls, value: str) -> str:
        value = value.lower()[:3]
        if value not in {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}:
            raise ValueError("SCHEDULE_DAY must be a weekday name")
        return value

    @field_validator("schedule_hour")
    @classmethod
    def valid_hour(cls, value: int) -> int:
        if not 0 <= value <= 23:
            raise ValueError("SCHEDULE_HOUR must be between 0 and 23")
        return value

    @field_validator("schedule_minute")
    @classmethod
    def valid_minute(cls, value: int) -> int:
        if not 0 <= value <= 59:
            raise ValueError("SCHEDULE_MINUTE must be between 0 and 59")
        return value

    @field_validator("manager_email")
    @classmethod
    def valid_manager_email(cls, value: str) -> str:
        value = value.strip()
        if value and ("@" not in value or value.startswith("@") or value.endswith("@")):
            raise ValueError("MANAGER_EMAIL must be a valid email address")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
