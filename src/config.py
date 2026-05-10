"""配置与 SQLModel db engine"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlmodel import SQLModel, create_engine

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ROOT / ".env"), env_file_encoding="utf-8", extra="ignore"
    )

    wechat_app_id: str = Field(min_length=1)
    wechat_token: str = Field(min_length=3, max_length=32)
    wechat_aes_key: str = Field(default="")
    host: str = "0.0.0.0"
    port: int = 8000
    db_url: str = f"sqlite:///{ROOT / 'data' / 'app.db'}"
    session_ttl: int = 300
    max_fails: int = 5
    auth_ttl: int = 7 * 24 * 3600
    cookie_name: str = "auth_token"
    cookie_secure: bool = False  # http 部署默认 false；生产 https 改 true
    cookie_samesite: str = "lax"

    @model_validator(mode="after")
    def _check_aes(self):
        if self.wechat_aes_key and len(self.wechat_aes_key) != 43:
            raise ValueError("WECHAT_AES_KEY 必须 43 位")
        return self


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    Path(ROOT / "data").mkdir(parents=True, exist_ok=True)
    return s


engine = create_engine(
    get_settings().db_url,
    connect_args={"check_same_thread": False, "timeout": 10},
)


def init_db() -> None:
    SQLModel.metadata.create_all(engine)
    with engine.connect() as c:
        c.exec_driver_sql("PRAGMA journal_mode=WAL")
        c.exec_driver_sql("PRAGMA synchronous=NORMAL")

