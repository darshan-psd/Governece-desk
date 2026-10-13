"""Data access for credentials, single-use tokens, sign-in attempts and security events."""
from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import and_, delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import Conflict
from app.core.jsvalues import now_iso
from app.models import AuthToken, LoginAttempt, Membership, Organization, SecurityEvent, User
from app.repositories.sessions import iso_in, token_hash


class AuthRepository:
    def __init__(self, session: Session):
        self.session = session

    # Users ------------------------------------------------------------------------------------
    def user_by_email(self, email: str) -> User | None:
        return self.session.scalars(select(User).where(func.lower(User.email) == email.lower())).first()

    def get_user(self, user_id: str) -> User | None:
        return self.session.get(User, user_id)

    def set_email(self, user_id: str, email: str) -> None:
        try:
            self.session.execute(update(User).where(User.id == user_id).values(email=email, updated_at=now_iso()))
        except IntegrityError as error:  # ux_users_email: another request claimed it first
            raise Conflict("This email address already belongs to another account.") from error

    def set_password(self, user_id: str, password_hash: str, must_change_password: bool = False) -> None:
        """``must_change_password``: True for a temporary password (Owner-set); False clears it."""
        now = now_iso()
        self.session.execute(update(User).where(User.id == user_id).values(
            password_hash=password_hash, password_changed_at=now, updated_at=now, must_change_password=must_change_password))

    def mark_signed_in(self, user_id: str) -> None:
        self.session.execute(update(User).where(User.id == user_id).values(last_sign_in_at=now_iso()))

    def active_memberships(self, user_id: str) -> list[tuple[Membership, Organization]]:
        rows = self.session.execute(
            select(Membership, Organization).join(Organization, Organization.id == Membership.organization_id)
            .where(Membership.user_id == user_id, Membership.status == "active", Organization.status == "active")
            .order_by(Organization.name, Organization.id))
        return [(m, o) for m, o in rows]

    # Single-use tokens -------------------------------------------------------------------------
    def create_token(self, token: str, purpose: str, user_id: str, minutes: int, membership_id: str | None = None) -> None:
        # A new token replaces any unused token of the same purpose for the user.
        self.session.execute(update(AuthToken).where(AuthToken.user_id == user_id, AuthToken.purpose == purpose, AuthToken.used_at.is_(None))
                             .values(used_at=now_iso()))
        self.session.add(AuthToken(token_hash=token_hash(token), purpose=purpose, user_id=user_id, membership_id=membership_id,
                                   created_at=now_iso(), expires_at=iso_in(minutes * 60)))
        self.session.flush()

    def consume_token(self, token: str, purpose: str) -> AuthToken | None:
        """Marks the token used and returns it, or None when unknown, used or expired (atomic)."""
        hashed = token_hash(token)
        now = now_iso()
        result = self.session.execute(
            update(AuthToken).where(AuthToken.token_hash == hashed, AuthToken.purpose == purpose,
                                    AuthToken.used_at.is_(None), AuthToken.expires_at > now)
            .values(used_at=now).execution_options(synchronize_session=False))
        if result.rowcount != 1:
            return None
        return self.session.get(AuthToken, hashed)

    # Throttling --------------------------------------------------------------------------------
    def failures_since(self, scope: str, key: str, since: str) -> int:
        return self.session.execute(select(func.count()).select_from(LoginAttempt).where(
            and_(LoginAttempt.scope == scope, LoginAttempt.key == key, LoginAttempt.at > since, LoginAttempt.success == 0))).scalar_one()

    def record_attempt(self, scope: str, key: str, success: bool) -> None:
        self.session.add(LoginAttempt(scope=scope, key=key, at=now_iso(), success=1 if success else 0))
        self.session.flush()

    def clear_failures(self, scope: str, key: str) -> None:
        self.session.execute(delete(LoginAttempt).where(LoginAttempt.scope == scope, LoginAttempt.key == key, LoginAttempt.success == 0))

    def purge_attempts(self, before: str) -> None:
        self.session.execute(delete(LoginAttempt).where(LoginAttempt.at < before))

    # Security events ---------------------------------------------------------------------------
    def event(self, event: str, *, organization_id: str | None = None, user_id: str | None = None,
              membership_id: str | None = None, client: str | None = None, **detail: Any) -> None:
        self.session.add(SecurityEvent(
            id=str(uuid.uuid4()), at=now_iso(), event=event, organization_id=organization_id, user_id=user_id,
            membership_id=membership_id, client=client,
            detail=json.dumps(detail, ensure_ascii=False, separators=(",", ":")) if detail else None))
        self.session.flush()

    def events_for_organization(self, organization_id: str, limit: int, offset: int) -> tuple[list[dict], int]:
        condition = SecurityEvent.organization_id == organization_id
        rows = self.session.scalars(select(SecurityEvent).where(condition).order_by(SecurityEvent.at.desc(), SecurityEvent.id)
                                    .limit(limit).offset(offset))
        total = self.session.execute(select(func.count()).select_from(SecurityEvent).where(condition)).scalar_one()
        items = [{"id": r.id, "at": r.at, "event": r.event, "membership": r.membership_id, "client": r.client,
                  "detail": json.loads(r.detail) if r.detail else {}} for r in rows]
        return items, total
