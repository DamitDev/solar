"""Pydantic models for maintainer status messages."""

from datetime import datetime, timezone
from typing import Literal, Self

from pydantic import BaseModel, Field, field_validator, model_validator

MessageType = Literal["incident", "maintenance", "planned maintenance", "resolution"]


def _as_utc(value: datetime | None) -> datetime | None:
    """Treat naive datetimes as UTC so comparisons never mix naive and aware."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _check_window(start_time: datetime | None, end_time: datetime | None) -> None:
    if start_time is not None and end_time is not None and start_time > end_time:
        raise ValueError("start_time must not be after end_time")


class StatusMessageCreate(BaseModel):
    body: str = Field(min_length=1)
    type: MessageType
    start_time: datetime
    end_time: datetime | None = None

    @model_validator(mode="after")
    def _validate_window(self) -> Self:
        self.start_time = _as_utc(self.start_time)
        self.end_time = _as_utc(self.end_time)
        _check_window(self.start_time, self.end_time)
        return self


class StatusMessageUpdate(BaseModel):
    """Partial update; an explicit ``end_time: null`` makes the window open-ended."""

    body: str | None = Field(default=None, min_length=1)
    type: MessageType | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None

    @field_validator("start_time", "end_time")
    @classmethod
    def _to_utc(cls, value: datetime | None) -> datetime | None:
        return _as_utc(value)

    @model_validator(mode="after")
    def _validate_window(self) -> Self:
        # Normalising happens in the field validator: assigning here would mark
        # both fields as set and turn a partial update into an explicit window.
        _check_window(self.start_time, self.end_time)
        return self


class StatusMessageResponse(BaseModel):
    id: str
    body: str
    type: MessageType
    start_time: datetime
    end_time: datetime | None = None
    created_at: datetime | None = None
