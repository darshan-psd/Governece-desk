"""Local-preview sign-in provider (development and test only).

* Opening the app page signs a browser without a valid session in as the Owner membership of the
  preview organization (``GOVERNANCE_WORKSPACE_ID``).
* "Preview as" switches to another active membership **of the same organization**.

It is not authentication: anyone who can open the page is the Owner. The settings validator
refuses ``GOVERNANCE_AUTH_MODE=local-preview`` in staging/production, and both entry points also
refuse to run there, so this provider can never become a production bypass.
"""
from __future__ import annotations

from fastapi import Request, Response

from app.core.container import Services
from app.core.errors import Forbidden, NotFound
from app.repositories.identity import IdentityRepository


def _enabled(services: Services) -> bool:
    settings = services.settings
    return settings.local_preview and not settings.is_deployed


def ensure_local_session(services: Services, request: Request, response: Response) -> bool:
    """Returns True when a new preview session (and its cookies) was issued on ``response``."""
    from app.auth import client_address, current_principal, issue_session, set_session_cookie

    if not _enabled(services):
        return False
    principal = current_principal(services, request)
    if principal is not None:
        with services.db.read() as session:
            resolved = IdentityRepository(session).resolve(principal.membership_id)
        if resolved is not None and resolved.active:
            return False  # keep the browser's current (possibly switched) profile
    with services.db.read() as session:
        owner = IdentityRepository(session).membership_by_member(services.settings.governance_workspace_id, "owner")
    if owner is None or owner.status != "active":
        return False  # nothing to sign in as; API calls stay 401
    token = issue_session(services, owner.id, method="preview", client=client_address(services.settings, request),
                          user_agent=request.headers.get("user-agent"))
    set_session_cookie(response, services.settings, token)
    return True


def switch_profile(services: Services, request: Request, requested) -> str:
    """``Preview as``: a new session for another active membership of the caller's organization."""
    from app.auth import issue_session, require_principal

    if not _enabled(services):
        raise NotFound("Not found.")
    principal = require_principal(services, request)
    with services.db.read() as session:
        repo = IdentityRepository(session)
        current = repo.resolve(principal.membership_id)
        if current is None or not current.active:
            raise Forbidden("This workspace profile is not active.")
        target = repo.membership_by_member(current.organization_id, requested) if isinstance(requested, str) else None
        resolved = repo.resolve(target.id) if target is not None else None
    if resolved is None or not resolved.active:
        raise Forbidden("Unknown local profile")
    return issue_session(services, resolved.membership_id, method="preview")
