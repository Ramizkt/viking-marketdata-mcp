from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    viking_ws_url: str = "wss://bot.fkviking.com/ws"
    viking_request_timeout_seconds: float = Field(default=45.0, gt=1, le=300)

    public_base_url: str = ""
    railway_public_domain: str = ""
    port: int = Field(default=8000, ge=1, le=65535)

    inline_max_rows: int = Field(default=500, ge=1, le=100_000)
    inline_max_bytes: int = Field(default=200_000, ge=1_000, le=4_000_000)
    max_points_per_field: int = Field(default=500_000, ge=1_000, le=5_000_000)
    export_ttl_seconds: int = Field(default=86_400, ge=300, le=2_592_000)
    export_dir: Path = Path("./data/exports")
    export_signing_key: str = ""

    @property
    def resolved_public_base_url(self) -> str:
        if self.public_base_url:
            return self.public_base_url.rstrip("/")
        if self.railway_public_domain:
            return f"https://{self.railway_public_domain}".rstrip("/")
        return f"http://127.0.0.1:{self.port}"

@lru_cache
def get_settings() -> Settings:
    return Settings()
