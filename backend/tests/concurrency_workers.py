"""Worker functions run in separate OS processes (multiprocessing "spawn") by tests/test_postgres.py.

Each process builds its own application services (own engine and connection pool), like separate
uvicorn workers or servers would, then waits on a barrier so all of them hit the database at once.
They return HTTP-like status codes so the test can count winners and losers.
"""
from __future__ import annotations

from pathlib import Path


def _services(url: str, data_dir: str):
    from app.core.config import Settings
    from app.core.container import Services

    settings = Settings(governance_environment="test", governance_data_dir=Path(data_dir), governance_database_url=url,
                        governance_reminders_enabled=False, governance_auto_migrate=False, _env_file=None)
    return Services.build(settings)


def _owner_context(services, organization: str = "local-preview"):
    from app.auth import Principal
    from app.repositories.identity import IdentityRepository
    from app.services.context import load_context

    with services.db.read() as session:
        membership = IdentityRepository(session).membership_by_member(organization, "owner")
    return load_context(services, Principal(membership.id))


def _status(call) -> int:
    from app.core.errors import ApiError

    try:
        call()
        return 200
    except ApiError as error:
        return error.status_code


def issue_invoice(url: str, data_dir: str, barrier, company: str, invoice: str, revision: int) -> int:
    from app.services.portal import billing

    services = _services(url, data_dir)
    ctx = _owner_context(services)
    barrier.wait()
    try:
        return _status(lambda: billing.invoice_issue(services, ctx, {"id": invoice, "revision": revision}, ctx.find(company)))
    finally:
        services.db.dispose()


def report_payment(url: str, data_dir: str, barrier, company: str, invoice: str, revision: int, evidence: str, reference: str) -> int:
    from app.services.portal import billing

    services = _services(url, data_dir)
    ctx = _owner_context(services)
    body = {"id": invoice, "revision": revision, "amount": "10", "reference": reference, "paidAt": "2026-01-15", "evidence": [evidence]}
    barrier.wait()
    try:
        return _status(lambda: billing.payment_report(services, ctx, body, ctx.find(company)))
    finally:
        services.db.dispose()


def edit_task(url: str, data_dir: str, barrier, task: str, revision: int, n: int) -> int:
    from app.services import records_service
    from app.services.context import target

    services = _services(url, data_dir)
    ctx = _owner_context(services)
    barrier.wait()
    try:
        def run():
            body = {"id": task, "revision": revision, "data": {"title": f"Writer {n}"}}
            records_service.edit(services, ctx, body, target(ctx, body))
        return _status(run)
    finally:
        services.db.dispose()


def take_lease(url: str, data_dir: str, barrier, holder: str) -> bool:
    from app.repositories.identity import IdentityRepository

    services = _services(url, data_dir)
    barrier.wait()
    try:
        with services.db.write() as session:
            return IdentityRepository(session).acquire_lease("race", holder, 60)
    finally:
        services.db.dispose()


def migrate(url: str, barrier) -> str:
    import sqlalchemy as sa

    from app.database.migrations import current_revision, upgrade_to_head

    engine = sa.create_engine(url, poolclass=sa.pool.NullPool)
    barrier.wait()
    try:
        upgrade_to_head(engine)
        return current_revision(engine)
    finally:
        engine.dispose()


def _member_context(services, member_id: str, organization: str = "local-preview"):
    from app.auth import Principal
    from app.repositories.identity import IdentityRepository
    from app.services.context import load_context

    with services.db.read() as session:
        membership = IdentityRepository(session).membership_by_member(organization, member_id)
    return load_context(services, Principal(membership.id))


def import_trial_balance(url: str, data_dir: str, barrier, company: str, content: bytes) -> int:
    from app.services import documents_service
    from app.services.documents_service import IncomingFile

    services = _services(url, data_dir)
    ctx = _owner_context(services)
    form = {"company": company, "period": "2026-09", "currency": "INR"}
    barrier.wait()
    try:
        return _status(lambda: documents_service.import_trial_balance(services, ctx, form, IncomingFile("tb.csv", content)))
    finally:
        services.db.dispose()


def deactivate_member(url: str, data_dir: str, barrier, actor: str, target: str) -> int:
    from app.services import auth_service

    services = _services(url, data_dir)
    ctx = _member_context(services, actor)
    barrier.wait()
    try:
        return _status(lambda: auth_service.set_member_status(services, ctx, target, "inactive", "test"))
    finally:
        services.db.dispose()


def upload_new_version(url: str, data_dir: str, barrier, company: str, previous: str) -> int:
    from app.services import documents_service
    from app.services.documents_service import IncomingFile

    services = _services(url, data_dir)
    ctx = _owner_context(services)
    barrier.wait()
    try:
        return _status(lambda: documents_service.upload_document(services, ctx, {"company": company, "previous": previous},
                                                                 IncomingFile("v2.txt", b"version two")))
    finally:
        services.db.dispose()


def edit_title(url: str, data_dir: str, barrier, task: str, revision: int, title: str) -> int:
    from app.services import records_service
    from app.services.context import target

    services = _services(url, data_dir)
    ctx = _owner_context(services)
    barrier.wait()
    try:
        def run():
            body = {"id": task, "revision": revision, "data": {"title": title}}
            records_service.edit(services, ctx, body, target(ctx, body))
        return _status(run)
    finally:
        services.db.dispose()


def start_application(url: str, data_dir: str, barrier) -> str:
    """What every worker process does at start-up: migrate (if needed) and bootstrap the preview."""
    from app.core.config import Settings
    from app.core.container import Services
    from app.services.identity_service import ensure_preview_organization

    settings = Settings(governance_environment="test", governance_data_dir=Path(data_dir), governance_database_url=url,
                        governance_reminders_enabled=False, _env_file=None)
    barrier.wait()
    services = Services.build(settings)
    try:
        ensure_preview_organization(services)
        return "started"
    finally:
        services.db.dispose()
