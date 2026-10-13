"""Shared application services (settings, database, storage, mail), created once per app."""
from __future__ import annotations

from dataclasses import dataclass, field

from app.core.config import Settings
from app.database import Database
from app.notifications.mail import MailProvider, build_mailer
from app.storage import LocalFileStorage


@dataclass
class Services:
    settings: Settings
    db: Database
    storage: LocalFileStorage
    mailer: MailProvider = field(default=None)  # type: ignore[assignment]

    @classmethod
    def build(cls, settings: Settings) -> "Services":
        db = Database(settings.database_url, settings)
        db.prepare_schema(auto_migrate=settings.auto_migrate)
        return cls(settings=settings, db=db, storage=LocalFileStorage(settings.files_dir), mailer=build_mailer(settings))
