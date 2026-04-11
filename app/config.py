from __future__ import annotations

from typing import List
from urllib.parse import quote_plus

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        case_sensitive=False,
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # General
    PROJECT_NAME: str = "CD AI 后端服务"
    VERSION: str = "0.1.0"
    DESCRIPTION: str = "CD AI 后端 API 文档"

    # CORS - keep as raw string to avoid dotenv JSON decode issues; will parse after init
    CORS_ORIGINS: str = "*"

    # Server
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    DEBUG: bool = False
    RELOAD: bool = True

    # Database (can provide full DATABASE_URL or MYSQL_* parts)
    DATABASE_URL: str | None = None
    MYSQL_HOST: str = "127.0.0.1"
    MYSQL_PORT: int = 3306
    MYSQL_USER: str 
    MYSQL_PASSWORD: str 
    MYSQL_DATABASE: str
    # Auth
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    SECRET_KEY: SecretStr = SecretStr("change-me")
    ALGORITHM: str = "HS256"

    def parse_cors(self) -> List[str]:
        v = self.CORS_ORIGINS
        if v is None:
            return ["*"]
        if isinstance(v, str):
            if not v:
                return ["*"]
            if v.strip() == "*":
                return ["*"]
            return [p.strip() for p in v.split(",") if p.strip()]
        if isinstance(v, list):
            return v
        return ["*"]

    def build_database_url(self) -> str:
        if self.DATABASE_URL:
            return self.DATABASE_URL
        return (
            f"mysql+pymysql://{quote_plus(self.MYSQL_USER)}:{quote_plus(self.MYSQL_PASSWORD)}"
            f"@{self.MYSQL_HOST}:{self.MYSQL_PORT}/{self.MYSQL_DATABASE}?charset=utf8mb4"
        )


# Instantiate settings and compute DATABASE_URL if not provided
_settings = Settings()
# parse CORS origins into a list and compute DATABASE_URL if needed
cors_list = _settings.parse_cors()
settings = _settings.model_copy(update={"DATABASE_URL": _settings.build_database_url(), "CORS_ORIGINS": cors_list})

