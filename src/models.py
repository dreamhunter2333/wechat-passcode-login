"""SQLModel models"""
from __future__ import annotations

from typing import Optional

from sqlmodel import Field, SQLModel


class LoginSession(SQLModel, table=True):
    __tablename__ = "login_sessions"
    session_id: str = Field(primary_key=True)
    code: str = Field(index=True, unique=True)
    created_at: float = Field(index=True)
    status: str = Field(default="pending")  # pending | scanned | issued
    openid: Optional[str] = Field(default=None)


class FailCount(SQLModel, table=True):
    __tablename__ = "fail_counts"
    openid: str = Field(primary_key=True)
    count: int = Field(default=0)
    updated_at: float = Field(default=0.0)


class AuthSession(SQLModel, table=True):
    __tablename__ = "auth_sessions"
    token_hash: str = Field(primary_key=True)
    openid: str = Field(index=True)
    created_at: float
    expires_at: float = Field(index=True)
