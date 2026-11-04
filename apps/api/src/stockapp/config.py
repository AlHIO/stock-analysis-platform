from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file="../../.env", extra="ignore")
    database_url: str = "postgresql://stockapp:local_dev_only@localhost:5432/stockapp"
    redis_url: str = "redis://localhost:6379/0"
    ingestion_min_interval_seconds: float = Field(default=5.0, ge=1.0, le=300.0)
    ingestion_max_attempts: int = Field(default=3, ge=1, le=5)
    ingestion_max_response_bytes: int = Field(default=20_000_000, ge=1024, le=100_000_000)
