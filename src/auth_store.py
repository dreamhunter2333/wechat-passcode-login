"""登录态：cookie token <-> openid，token 存 SHA-256 hash"""
from __future__ import annotations

import hashlib
import secrets
import time
from threading import Lock
from typing import Optional

from sqlmodel import Session, select

from .config import engine
from .models import AuthSession


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class AuthStore:
    def __init__(self, ttl: int = 7 * 24 * 3600):
        self.ttl = ttl
        self._lock = Lock()

    def _gc(self, s: Session) -> None:
        for row in s.exec(select(AuthSession).where(AuthSession.expires_at < time.time())).all():
            s.delete(row)

    def create(self, openid: str) -> tuple[str, AuthSession]:
        """返回 (raw_token, row)。raw_token 仅此一次返回，db 只存 hash。"""
        token = secrets.token_urlsafe(32)
        with self._lock, Session(engine) as s:
            self._gc(s)
            now = time.time()
            row = AuthSession(
                token_hash=_hash(token),
                openid=openid,
                created_at=now,
                expires_at=now + self.ttl,
            )
            s.add(row)
            s.commit()
            s.refresh(row)
            return token, row

    def revoke_openid(self, openid: str) -> int:
        """撤销同 openid 的所有旧 session，返回撤销数。"""
        with self._lock, Session(engine) as s:
            rows = s.exec(select(AuthSession).where(AuthSession.openid == openid)).all()
            for r in rows:
                s.delete(r)
            s.commit()
            return len(rows)

    def get(self, token: str) -> Optional[AuthSession]:
        if not token:
            return None
        h = _hash(token)
        with Session(engine) as s:
            row = s.exec(select(AuthSession).where(AuthSession.token_hash == h)).first()
            if not row:
                return None
            if row.expires_at < time.time():
                s.delete(row)
                s.commit()
                return None
            return row

    def touch(self, token: str) -> Optional[float]:
        """续期，返回新 expires_at；不存在返回 None。"""
        if not token:
            return None
        h = _hash(token)
        with self._lock, Session(engine) as s:
            row = s.exec(select(AuthSession).where(AuthSession.token_hash == h)).first()
            if not row:
                return None
            row.expires_at = time.time() + self.ttl
            s.add(row)
            s.commit()
            return row.expires_at

    def revoke(self, token: str) -> None:
        if not token:
            return
        h = _hash(token)
        with self._lock, Session(engine) as s:
            row = s.exec(select(AuthSession).where(AuthSession.token_hash == h)).first()
            if row:
                s.delete(row)
                s.commit()
