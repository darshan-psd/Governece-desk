"""Server-side login sessions. Only a SHA-256 hash of each cookie token is stored."""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.core.jsvalues import now_iso
from app.models import AuthSession


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class SessionRepository:
    def __init__(self, session: Session):
        self.session = session

    def create(self, token: str, workspace: str, actor: str, hours: int) -> None:
        expires = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat(timespec="milliseconds")
        self.session.add(
            AuthSession(token_hash=token_hash(token), workspace=workspace, actor=actor,
                        created=now_iso(), expires=expires.replace("+00:00", "Z"))
        )
        self.session.flush()

    def find(self, token: str) -> AuthSession | None:
        row = self.session.get(AuthSession, token_hash(token))
        if row is None or row.expires <= now_iso():
            return None
        return row

    def purge_expired(self) -> None:
        self.session.execute(delete(AuthSession).where(AuthSession.expires <= now_iso()))
