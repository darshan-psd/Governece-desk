"""Server-side login sessions. Only a SHA-256 hash of each cookie token is stored."""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, or_, select, update
from sqlalchemy.orm import Session

from app.core.jsvalues import now_iso
from app.models import AuthSession


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def iso_in(seconds: float) -> str:
    moment = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class SessionRepository:
    def __init__(self, session: Session):
        self.session = session

    def create(self, token: str, workspace: str, actor: str, hours: int, membership_id: str | None = None, *,
               user_id: str | None = None, method: str = "preview", idle_minutes: int | None = None,
               client: str | None = None, user_agent: str | None = None) -> AuthSession:
        now = now_iso()
        row = AuthSession(
            token_hash=token_hash(token), workspace=workspace, actor=actor, membership_id=membership_id,
            created=now, expires=iso_in(hours * 3600), public_id=uuid.uuid4().hex, user_id=user_id, method=method,
            last_seen_at=now, idle_expires=iso_in(idle_minutes * 60) if idle_minutes else None,
            client=client, user_agent=(user_agent or "")[:200] or None,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def find(self, token: str) -> AuthSession | None:
        """The session if it exists, is not revoked and has not expired (absolute or idle)."""
        row = self.session.get(AuthSession, token_hash(token))
        now = now_iso()
        if row is None or row.revoked_at is not None or row.expires <= now:
            return None
        if row.idle_expires is not None and row.idle_expires <= now:
            return None
        return row

    def touch(self, token: str, idle_minutes: int) -> None:
        """Slides the idle timeout forward (never past the absolute expiry, which ``find`` checks)."""
        self.session.execute(
            update(AuthSession).where(AuthSession.token_hash == token_hash(token), AuthSession.revoked_at.is_(None))
            .values(last_seen_at=now_iso(), idle_expires=iso_in(idle_minutes * 60)).execution_options(synchronize_session=False))

    def revoke(self, token: str) -> None:
        self.session.execute(update(AuthSession).where(AuthSession.token_hash == token_hash(token), AuthSession.revoked_at.is_(None))
                             .values(revoked_at=now_iso()).execution_options(synchronize_session=False))

    def revoke_public(self, user_id: str, public_id: str) -> bool:
        result = self.session.execute(
            update(AuthSession).where(AuthSession.user_id == user_id, AuthSession.public_id == public_id, AuthSession.revoked_at.is_(None))
            .values(revoked_at=now_iso()).execution_options(synchronize_session=False))
        return result.rowcount == 1

    def revoke_user(self, user_id: str, except_token: str | None = None) -> int:
        """Ends every session of a user (all organizations), optionally keeping the current one."""
        statement = update(AuthSession).where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
        if except_token:
            statement = statement.where(AuthSession.token_hash != token_hash(except_token))
        return self.session.execute(statement.values(revoked_at=now_iso()).execution_options(synchronize_session=False)).rowcount

    def revoke_membership(self, membership_id: str) -> int:
        return self.session.execute(
            update(AuthSession).where(AuthSession.membership_id == membership_id, AuthSession.revoked_at.is_(None))
            .values(revoked_at=now_iso()).execution_options(synchronize_session=False)).rowcount

    def active_for_user(self, user_id: str) -> list[AuthSession]:
        now = now_iso()
        return list(self.session.scalars(
            select(AuthSession).where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None), AuthSession.expires > now,
                                      or_(AuthSession.idle_expires.is_(None), AuthSession.idle_expires > now))
            .order_by(AuthSession.created.desc())))

    def purge_expired(self) -> None:
        """Deletes sessions that ended more than a day ago (revoked ones are kept a day for inspection)."""
        cutoff = iso_in(-86400)
        self.session.execute(delete(AuthSession).where(or_(
            AuthSession.expires <= now_iso(), AuthSession.revoked_at < cutoff, AuthSession.idle_expires <= cutoff)))
