"""Authentication boundary: who is calling, resolved only from server-side state.

Layers:
* **Sessions** (this module): an opaque HttpOnly cookie maps to a server-side session bound to one
  organization membership. Organization, role and company scope are always read from the
  membership tables; nothing the browser sends (headers, bodies, ids) can choose the tenant.
* **Sign-in providers** issue sessions: ``app.auth.preview`` (development/test only, refused in
  staging/production) and ``app.services.auth_service`` (email + password).
* **CSRF**: every state-changing ``/api/`` request must carry ``X-CSRF-Token``. With a session, the
  token is derived from the session token (``csrf_for``) and handed to the page in a readable
  cookie; another site can neither read that cookie nor compute the value. Without a session
  (the sign-in form) a random double-submit cookie is used. See ``app.core.csrf``.

Fail closed: a missing, unknown, expired, idle, revoked or unlinked session is 401; an inactive
organization, user or membership is 403.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass, field

from fastapi import Request, Response

from app.core.config import Settings
from app.core.container import Services
from app.core.errors import Forbidden, Unauthorized
from app.repositories.identity import IdentityRepository
from app.repositories.sessions import SessionRepository, iso_in

COOKIE_NAME = "gov_preview"  # local-preview session cookie (unchanged)
CSRF_HEADER = "x-csrf-token"
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
SIGN_IN_MESSAGE = "Please sign in to access your workspace."
CSRF_MESSAGE = "Your security token is missing or out of date. Reload the page and try again."
# Display profile of an organization's built-in owner when no profile record exists yet.
BUILTIN_OWNER = {"id": "owner", "name": "Workspace owner", "role": "Owner", "companies": ["*"], "active": True}
TOUCH_SECONDS = 60


def session_cookie_name(settings: Settings) -> str:
    if settings.local_preview:
        return COOKIE_NAME
    # ``__Host-``: the browser only accepts it over HTTPS, for this exact host, path=/.
    return "__Host-gov_session" if settings.secure_cookies else "gov_session"


def csrf_cookie_name(settings: Settings) -> str:
    return "__Host-gov_csrf" if settings.secure_cookies else "gov_csrf"


def csrf_for(session_token: str) -> str:
    """The CSRF token of a session: public, but only computable by someone holding the session token."""
    return hashlib.sha256(b"governance-csrf:" + session_token.encode()).hexdigest()


def client_address(settings: Settings, request: Request) -> str:
    """Caller address: the socket peer, or the entry our own N proxies appended to X-Forwarded-For."""
    hops = settings.governance_forwarded_hops
    if hops:
        forwarded = [part.strip() for part in request.headers.get("x-forwarded-for", "").split(",") if part.strip()]
        if len(forwarded) >= hops:
            return forwarded[-hops][:64]
    return (request.client.host if request.client else "")[:64]


@dataclass(frozen=True)
class Principal:
    """A session's identity. Only ``membership_id`` is trusted; the rest is resolved from it."""

    membership_id: str
    token: str | None = field(default=None, repr=False, compare=False)
    session_id: str | None = None
    user_id: str | None = None
    method: str | None = None


def _session_token(services: Services, request: Request) -> str | None:
    return request.cookies.get(session_cookie_name(services.settings)) or None


def current_principal(services: Services, request: Request) -> Principal | None:
    settings = services.settings
    token = _session_token(services, request)
    if not token:
        return None
    with services.db.read() as session:
        row = SessionRepository(session).find(token)
        if row is None:
            return None
        # Password mode accepts only sessions created by a password sign-in: a session issued by the
        # development preview (or before Phase 3) can never act in it.
        if settings.password_auth and row.method != "password":
            return None
        membership_id = row.membership_id
        if not membership_id:
            # Session created before memberships existed: usable only if it maps to exactly one membership.
            membership = IdentityRepository(session).membership_by_member(row.workspace, row.actor)
            membership_id = membership.id if membership else None
        if membership_id is None:
            return None
        needs_touch = row.idle_expires is not None and (row.last_seen_at or "") < iso_in(-TOUCH_SECONDS)
        principal = Principal(membership_id, token=token, session_id=row.public_id, user_id=row.user_id, method=row.method)
    if needs_touch:
        with services.db.write() as session:
            SessionRepository(session).touch(token, settings.governance_session_idle_minutes)
    return principal


def require_principal(services: Services, request: Request) -> Principal:
    principal = current_principal(services, request)
    if principal is None:
        raise Unauthorized(SIGN_IN_MESSAGE)
    if request.method not in SAFE_METHODS:
        sent = request.headers.get(CSRF_HEADER, "")
        if not sent or not hmac.compare_digest(sent, csrf_for(principal.token or "")):
            raise Forbidden(CSRF_MESSAGE)
    return principal


def issue_session(services: Services, membership_id: str, *, method: str = "preview", client: str | None = None,
                  user_agent: str | None = None) -> str:
    """Creates a session for a membership (any sign-in provider). Returns the cookie token."""
    settings = services.settings
    token = secrets.token_urlsafe(32)
    with services.db.write() as session:
        resolved = IdentityRepository(session).resolve(membership_id)
        if resolved is None or not resolved.active:
            raise Unauthorized(SIGN_IN_MESSAGE)
        repo = SessionRepository(session)
        repo.purge_expired()
        repo.create(token, resolved.organization_id, resolved.member_id, settings.governance_session_hours,
                    membership_id=resolved.membership_id, user_id=resolved.user_id, method=method,
                    idle_minutes=settings.governance_session_idle_minutes if method == "password" else None,
                    client=client, user_agent=user_agent)
    return token


def set_session_cookie(response: Response, settings: Settings, token: str) -> None:
    """Session cookie (HttpOnly) plus its CSRF cookie (readable by the page, never by other sites)."""
    secure = settings.secure_cookies
    max_age = settings.governance_session_hours * 3600
    response.set_cookie(session_cookie_name(settings), token, httponly=True, samesite="strict", path="/", secure=secure, max_age=max_age)
    response.set_cookie(csrf_cookie_name(settings), csrf_for(token), httponly=False, samesite="strict", path="/", secure=secure, max_age=max_age)


def clear_session_cookie(response: Response, settings: Settings) -> None:
    secure = settings.secure_cookies
    response.delete_cookie(session_cookie_name(settings), path="/", secure=secure, httponly=True, samesite="strict")
    # A fresh anonymous CSRF token, so the sign-in form keeps working after sign-out.
    response.set_cookie(csrf_cookie_name(settings), secrets.token_urlsafe(32), httponly=False, samesite="strict", path="/", secure=secure)


def ensure_csrf_cookie(services: Services, request: Request, response: Response) -> None:
    """Makes sure the page holds the right CSRF token: the session's, or an anonymous one."""
    settings = services.settings
    name = csrf_cookie_name(settings)
    present = request.cookies.get(name)
    token = _session_token(services, request)
    if token and current_principal(services, request) is not None:
        expected = csrf_for(token)
        if present != expected:
            response.set_cookie(name, expected, httponly=False, samesite="strict", path="/", secure=settings.secure_cookies,
                                max_age=settings.governance_session_hours * 3600)
    elif not present:
        response.set_cookie(name, secrets.token_urlsafe(32), httponly=False, samesite="strict", path="/", secure=settings.secure_cookies)


def revoke_current(services: Services, principal: Principal) -> None:
    if principal.token:
        with services.db.write() as session:
            SessionRepository(session).revoke(principal.token)


# The development preview provider; imported here so existing call sites keep working.
from app.auth.preview import ensure_local_session, switch_profile  # noqa: E402,F401
