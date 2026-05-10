"""6 位验证码 → 登录会话；惰性 GC（仅写路径）"""
from __future__ import annotations

import secrets
import time
from threading import Lock
from typing import Optional

from sqlmodel import Session, select

from .config import engine
from .models import FailCount, LoginSession


class SessionStore:
    def __init__(self, ttl: int = 300, max_fails: int = 5):
        self.ttl = ttl
        self.max_fails = max_fails
        self._write_lock = Lock()

    def _gc(self, s: Session) -> None:
        cutoff = time.time() - self.ttl
        for row in s.exec(select(LoginSession).where(LoginSession.created_at < cutoff)).all():
            s.delete(row)
        for row in s.exec(select(FailCount).where(FailCount.updated_at < time.time() - 600)).all():
            s.delete(row)

    def create(self) -> LoginSession:
        with self._write_lock, Session(engine) as s:
            self._gc(s)
            for _ in range(20):
                code = f"{secrets.randbelow(1_000_000):06d}"
                if not s.exec(select(LoginSession).where(LoginSession.code == code)).first():
                    break
            else:
                raise RuntimeError("code 冲突")
            row = LoginSession(
                session_id=secrets.token_urlsafe(24),
                code=code,
                created_at=time.time(),
                status="pending",
            )
            s.add(row)
            s.commit()
            s.refresh(row)
            return row

    def get(self, session_id: str) -> Optional[LoginSession]:
        # 只读路径不做 GC
        with Session(engine) as s:
            row = s.exec(select(LoginSession).where(LoginSession.session_id == session_id)).first()
            if not row:
                return None
            if time.time() - row.created_at > self.ttl:
                return None
            return row

    def mark_issued(self, session_id: str) -> bool:
        """scanned -> issued，幂等：已 issued 返回 False。"""
        with self._write_lock, Session(engine) as s:
            row = s.exec(select(LoginSession).where(LoginSession.session_id == session_id)).first()
            if not row or row.status != "scanned":
                return False
            row.status = "issued"
            s.add(row)
            s.commit()
            return True

    def consume(self, code: str, openid: str) -> Optional[LoginSession]:
        with self._write_lock, Session(engine) as s:
            self._gc(s)
            fail = s.exec(select(FailCount).where(FailCount.openid == openid)).first()
            if fail and fail.count >= self.max_fails:
                return None
            row = s.exec(
                select(LoginSession).where(
                    LoginSession.code == code, LoginSession.status == "pending"
                )
            ).first()
            if not row:
                if fail:
                    fail.count += 1
                    fail.updated_at = time.time()
                else:
                    fail = FailCount(openid=openid, count=1, updated_at=time.time())
                s.add(fail)
                s.commit()
                return None
            row.status = "scanned"
            row.openid = openid
            s.add(row)
            if fail:
                s.delete(fail)
            s.commit()
            s.refresh(row)
            return row
