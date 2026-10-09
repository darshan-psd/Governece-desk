"""Shared application services (settings, database, storage), created once per app."""
from __future__ import annotations

from dataclasses import dataclass

from app.core.config import Settings
from app.database import Database
from app.storage import LocalFileStorage


@dataclass
class Services:
    settings: Settings
    db: Database
    storage: LocalFileStorage

    @classmethod
    def build(cls, settings: Settings) -> "Services":
        db = Database(settings.database_path)
        db.prepare_schema(auto_migrate=settings.auto_migrate)
        return cls(settings=settings, db=db, storage=LocalFileStorage(settings.files_dir))
