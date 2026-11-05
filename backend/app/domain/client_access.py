"""What a client user may do inside one company's client workspace.

``organization_memberships.role`` says what a person is to the firm. Everyone on a client's team is
a ``Client`` there, which keeps them out of all internal work, whatever their job title. Their
position inside a company lives on ``company_access``, one row per company, so the same person can
be a Company Admin of one company and have nothing at another. These client-side roles are separate
from the firm's own staff roles: a company's "Director" is not the firm's internal Director.

``client_role`` NULL marks a company's primary client contact: an account the firm created, or a
client who proposed the company themselves. They keep everything they could always do, and act as
its Company Admin. Every other role is explicit, so a restriction can never come from a default.

Permissions are decided here and enforced on the server (``app.permissions`` and the portal
services). The portal only uses them to decide what to show.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

COMPANY_ADMIN = "Company Admin"
DIRECTOR = "Director"
ACCOUNTANT = "Accountant"
EMPLOYEE = "Employee"
CLIENT_ROLES = (COMPANY_ADMIN, DIRECTOR, ACCOUNTANT, EMPLOYEE)

# Portal modules a Company Admin can assign to an Employee. Overview and the requests addressed to
# the person are not modules: being sent a request is itself what authorizes seeing it.
EMPLOYEE_MODULES = ("tracker", "profile", "onboarding", "documents", "inbox", "calendar", "invoices")
# Sharing payment proof needs an upload, so the invoices module brings the permission with it.
_MODULE_IMPLIES = {"invoices": ("documents.upload",)}
# What an Accountant may read of the company's shared documents.
FINANCIAL_CATEGORIES = ("Financial statements", "Tax & other registrations", "Invoices & payment proof")

_ROLE_PERMISSIONS = {
    COMPANY_ADMIN: frozenset({*EMPLOYEE_MODULES, "profile.edit", "documents.upload", "team"}),
    DIRECTOR: frozenset({"tracker", "profile", "onboarding", "documents", "calendar"}),
    ACCOUNTANT: frozenset({"documents.financial", "documents.upload", "invoices"}),
}


@dataclass(frozen=True)
class CompanyGrant:
    """A client user's position in one company."""

    role: str
    modules: tuple[str, ...]
    permissions: frozenset[str]


def parse_modules(stored: Any) -> tuple[str, ...]:
    """Assigned modules from their stored JSON. Anything unrecognised grants nothing."""
    try:
        values = json.loads(stored) if isinstance(stored, str) else []
    except ValueError:
        return ()
    return tuple(m for m in EMPLOYEE_MODULES if isinstance(values, list) and m in values)


def company_grant(client_role: str | None, stored_modules: Any = None) -> CompanyGrant:
    role = COMPANY_ADMIN if client_role is None else client_role
    if role == EMPLOYEE:
        modules = parse_modules(stored_modules)
        implied = {extra for module in modules for extra in _MODULE_IMPLIES.get(module, ())}
        return CompanyGrant(role, modules, frozenset({*modules, *implied}))
    # An unknown stored role grants nothing (the database constraint should make it impossible).
    return CompanyGrant(role, (), _ROLE_PERMISSIONS.get(role, frozenset()))
