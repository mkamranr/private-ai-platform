"""Shared response envelopes."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ORMModel(BaseModel):
    """Base for schemas built from ORM objects."""

    model_config = ConfigDict(from_attributes=True)


class Page[T](BaseModel):
    """Offset-paginated collection.

    Every list endpoint returns this shape from the start. Adding pagination to an
    endpoint that once returned a bare array is a breaking change for callers, and
    §M19's own audit and run listings will be far too large to return whole.
    """

    items: list[T]
    total: int = Field(description="Total rows matching the query, ignoring limit/offset")
    limit: int
    offset: int

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.items) < self.total


class ErrorDetail(BaseModel):
    code: str
    message: str
    request_id: str | None = None
    details: dict[str, Any] | None = None


class ErrorResponse(BaseModel):
    """The single error shape every endpoint returns (see app.core.errors)."""

    error: ErrorDetail


class MessageResponse(BaseModel):
    message: str
