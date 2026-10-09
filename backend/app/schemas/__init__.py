"""Pydantic models for query parameters of the paginated read endpoints."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.core.errors import BadRequest
from app.core.jsvalues import date_valid

SORTABLE = ("updated", "due", "title", "created", "status")


class Page(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int = Field(50, ge=1, le=500)
    offset: int = Field(0, ge=0)


class RecordQuery(Page):
    """``GET /api/v2/records``: every filter is applied by SQLite."""

    kind: list[str] = []
    company: list[str] = []
    status: list[str] = []
    assignee: str | None = None
    priority: str | None = None
    due_from: str | None = None
    due_to: str | None = None
    sort: Literal["updated", "due", "title", "created", "status"] = "updated"
    order: Literal["asc", "desc"] = "desc"

    @field_validator("kind", "company", "status", mode="before")
    @classmethod
    def _split(cls, value):
        """Accepts repeated parameters (?kind=a&kind=b) and comma lists (?kind=a,b)."""
        values = value if isinstance(value, list) else [value]
        return [part.strip() for v in values for part in str(v).split(",") if part.strip()]

    @field_validator("due_from", "due_to")
    @classmethod
    def _date(cls, value):
        if value is not None and not date_valid(value):
            raise ValueError("must be a date in YYYY-MM-DD format")
        return value


def parse_query(model: type[BaseModel], params) -> BaseModel:
    """Validates query parameters; errors use the API's usual ``{"error": ...}`` 400 response."""
    data: dict = {}
    for key in params.keys():
        values = params.getlist(key)
        list_field = key in ("kind", "company", "status")
        data[key] = values if list_field else values[-1]
    try:
        return model.model_validate(data)
    except ValidationError as error:
        problem = error.errors()[0]
        location = ".".join(str(p) for p in problem.get("loc", ())) or "query"
        raise BadRequest(f"Invalid query parameter {location}: {problem.get('msg')}") from None
