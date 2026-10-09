"""Role and company-scope checks, enforced on the server for every request."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from app.core.errors import Forbidden
from app.domain.workflow_rules import can_access
from app.repositories.records import RecordRepository

# Fields on company records that are internal to staff and must never reach a Client.
CLIENT_HIDDEN_COMPANY_FIELDS = ("comments", "approval")
_MISSING = object()


class WorkspaceStore:
    """Request-scoped reader for one workspace.

    Every lookup is a targeted SQL query (primary key or indexed filter). Records read during the
    request are cached by id, so repeated lookups of the same record cost no extra query. Nothing
    loads the whole workspace unless ``all()`` is called explicitly (full-state responses only).
    """

    def __init__(self, db, owner: str):
        self.db = db
        self.owner = owner
        self._by_id: dict[str, Any] = {}
        self._all: list[dict] | None = None

    def _run(self, method: str, *args, **kwargs):
        with self.db.read() as session:
            return getattr(RecordRepository(session, self.owner), method)(*args, **kwargs)

    def _remember(self, records: Iterable[dict]) -> None:
        for record in records:
            self._by_id[record["id"]] = record

    def get(self, record_id: Any) -> dict | None:
        if not isinstance(record_id, str):
            return None
        cached = self._by_id.get(record_id, _MISSING)
        if cached is _MISSING:
            cached = self._run("get", record_id)
            self._by_id[record_id] = cached
        return cached

    def get_many(self, ids: Iterable[Any]) -> dict[str, dict]:
        wanted = [i for i in ids if isinstance(i, str)]
        missing = [i for i in set(wanted) if i not in self._by_id]
        if missing:
            found = self._run("get_many", missing)
            for record_id in missing:
                self._by_id[record_id] = found.get(record_id)
        return {i: self._by_id[i] for i in wanted if self._by_id.get(i) is not None}

    def find(self, **filters) -> list[dict]:
        records = self._run("find", **filters)
        self._remember(records)
        return records

    def exists(self, **filters) -> bool:
        return self._run("exists", **filters)

    def count(self, **filters) -> int:
        return self._run("count", **filters)

    def json_rows(self, **filters) -> list[tuple[str, str, str]]:
        return self._run("json_rows", **filters)

    def all(self) -> list[dict]:
        if self._all is None:
            self._all = self._run("find")
            self._remember(self._all)
        return self._all


@dataclass
class Context:
    """Who is acting, in which workspace, and a query-backed view of the workspace's records."""

    owner: str
    actor: dict
    store: WorkspaceStore = field(repr=False, default=None)  # type: ignore[assignment]

    @property
    def role(self) -> str:
        return self.actor.get("role", "")

    @property
    def scoped_companies(self) -> list[str] | None:
        """Companies this actor is limited to, or ``None`` when it can see every company."""
        companies = self.actor.get("companies")
        companies = companies if isinstance(companies, list) else []
        if self.role == "Owner" or "*" in companies:
            return None
        return [c for c in companies if isinstance(c, str)]

    # Lookups (all SQL-backed, cached per request) -------------------------------------------
    def find(self, record_id, kind: str | None = None) -> dict | None:
        record = self.store.get(record_id)
        if record is None or (kind is not None and record.get("kind") != kind):
            return None
        return record

    def find_many(self, ids: Iterable[Any], kind: str | None = None) -> dict[str, dict]:
        found = self.store.get_many(ids)
        return {i: r for i, r in found.items() if kind is None or r.get("kind") == kind}

    def query(self, **filters) -> list[dict]:
        return self.store.find(**filters)

    def exists(self, **filters) -> bool:
        return self.store.exists(**filters)

    @property
    def records(self) -> list[dict]:
        """Every record of the workspace. Only full-state responses should need this."""
        return self.store.all()


def authorize(ctx: Context, roles: Iterable[str]) -> None:
    if ctx.role not in roles:
        raise Forbidden("Your role does not permit this action.")


def accessible(ctx: Context, record: dict) -> bool:
    """Whether the acting profile may see ``record`` (company scope + client restrictions)."""
    if not can_access(ctx.actor, record):
        return False
    if record.get("kind") == "briefing":
        for source in record.get("sources") or []:
            original = ctx.find(source.get("id") if isinstance(source, dict) else None)
            if not original or not can_access(ctx.actor, original):
                return False
    if ctx.role == "Client":
        kind = record.get("kind")
        return (
            kind == "company"
            or (kind == "request" and record.get("assignee") == ctx.actor.get("id"))
            or (kind == "document" and record.get("shared") is True)
        )
    return record.get("kind") != "member" or ctx.role != "Client"


def prefetch_briefing_sources(ctx: Context, records: Iterable[dict]) -> None:
    """Loads every briefing source in one batched query, so ``accessible()`` adds no per-record reads."""
    ctx.find_many(
        source.get("id")
        for record in records if record.get("kind") == "briefing"
        for source in (record.get("sources") or []) if isinstance(source, dict)
    )


def client_safe(ctx: Context, record: dict) -> dict:
    """Removes staff-only fields before a record is returned to a Client.

    Security fix: the Node backend's ``GET /api/v2`` returned internal staff comments and
    approval details on company records to Clients (the portal API already stripped them).
    """
    if ctx.role == "Client" and record.get("kind") == "company":
        return {k: v for k, v in record.items() if k not in CLIENT_HIDDEN_COMPANY_FIELDS}
    return record
