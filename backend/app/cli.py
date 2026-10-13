"""Operator commands: ``python -m app.cli <command>`` (run from ``backend/``).

  create-organization   New firm with its Owner account. The Owner's password is typed at a hidden
                        prompt, or ``--invite`` mails an activation link instead.
  set-credentials       Give an existing team profile (e.g. the preview workspace's ``owner``) an
                        email and a password typed at a hidden prompt.
  invite                Mail an activation link to an existing team profile.
  transfer-to-postgres  Copy a SQLite workspace database into the empty PostgreSQL database named by
                        GOVERNANCE_DATABASE_URL and reconcile every row (the SQLite file is only read).

Nothing here generates or prints passwords. Uses the database configured in the environment.
"""
from __future__ import annotations

import argparse
import getpass
import sys

from app.core.config import get_settings
from app.core.container import Services
from app.core.errors import ApiError


def _password(confirm: bool = True) -> str:
    password = getpass.getpass("Password (12+ characters, not shown): ")
    if confirm and getpass.getpass("Repeat password: ") != password:
        raise SystemExit("Passwords do not match.")
    return password


def _membership(services: Services, organization: str, member: str):
    from app.repositories.identity import IdentityRepository

    with services.db.read() as session:
        membership = IdentityRepository(session).membership_by_member(organization, member)
    if membership is None:
        raise SystemExit(f"No team profile {member!r} in organization {organization!r}.")
    return membership


def create_organization(services: Services, args) -> None:
    from app.services.identity_service import create_organization as create, set_credentials

    password = None if args.invite else _password()
    with services.db.write() as session:
        _organization, membership = create(session, args.id, args.name, args.owner_name)
        if password is not None:
            set_credentials(session, membership.id, args.owner_email, password)
    if args.invite:
        _invite(services, args.id, "owner", args.owner_email)
    print(f"Organization {args.id!r} created with its Owner account ({args.owner_email}).")


def _invite(services: Services, organization: str, member: str, email: str) -> None:
    from app.services import auth_service
    from app.services.context import load_context
    from app.auth import Principal

    if not services.settings.password_auth:
        raise SystemExit("Invitations need GOVERNANCE_AUTH_MODE=password.")
    owner = _membership(services, organization, "owner")
    ctx = load_context(services, Principal(owner.id))
    result = auth_service.invite_member(services, ctx, member, email, client="cli")
    where = f" (development outbox: {services.settings.outbox_dir})" if services.settings.governance_mail_provider == "outbox" else ""
    print("Activation link sent" + where if result["delivered"] else "The activation email could not be delivered; check the mail settings.")


def set_credentials(services: Services, args) -> None:
    from app.services.identity_service import set_credentials as apply

    membership = _membership(services, args.organization, args.member)
    password = _password()
    with services.db.write() as session:
        apply(session, membership.id, args.email, password)
    print(f"Credentials set for {args.member!r} in {args.organization!r}.")


def transfer_to_postgres(args) -> int:
    import json
    from pathlib import Path

    from app.database.transfer import transfer

    settings = get_settings()
    if not settings.uses_postgres:
        print("Set GOVERNANCE_DATABASE_URL to the target PostgreSQL database (kept out of the command line on purpose).", file=sys.stderr)
        return 2
    source = Path(args.source) if args.source else settings.database_path
    report = transfer(source, settings.database_url)
    text = json.dumps(report.as_dict(), indent=2, ensure_ascii=False)
    if args.report:
        Path(args.report).write_text(text + "\n", encoding="utf-8")
    print(text)
    print("Transfer verified: every row reconciled." if report.ok else "Transfer NOT verified; see problems above.", file=sys.stderr)
    return 0 if report.ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create-organization")
    create.add_argument("--id", required=True, help="organization id (tenant key), e.g. firm-a")
    create.add_argument("--name", required=True)
    create.add_argument("--owner-name", required=True)
    create.add_argument("--owner-email", required=True)
    create.add_argument("--invite", action="store_true", help="mail an activation link instead of prompting for a password")
    creds = commands.add_parser("set-credentials")
    creds.add_argument("--organization", required=True)
    creds.add_argument("--member", default="owner", help="team profile id (default: owner)")
    creds.add_argument("--email", required=True)
    invite = commands.add_parser("invite")
    invite.add_argument("--organization", required=True)
    invite.add_argument("--member", required=True)
    invite.add_argument("--email", required=True)
    move = commands.add_parser("transfer-to-postgres")
    move.add_argument("--source", help="SQLite database file (default: <GOVERNANCE_DATA_DIR>/preview.sqlite)")
    move.add_argument("--report", help="also write the reconciliation report (JSON) to this file")
    args = parser.parse_args(argv)
    if args.command == "transfer-to-postgres":
        return transfer_to_postgres(args)

    settings = get_settings()
    settings.validate_runtime()
    services = Services.build(settings)
    try:
        if args.command == "create-organization":
            create_organization(services, args)
        elif args.command == "set-credentials":
            set_credentials(services, args)
        else:
            _invite(services, args.organization, args.member, args.email)
    except ApiError as error:
        print(f"Error: {error.message}", file=sys.stderr)
        return 1
    finally:
        services.db.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
