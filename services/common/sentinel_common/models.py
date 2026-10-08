from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class Level(str, Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class EventIn(BaseModel):
    """A single log/telemetry event sent by a monitored service."""

    service: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")
    level: Level = Level.INFO
    message: str = Field(default="", max_length=2000)
    latency_ms: float | None = Field(default=None, ge=0, le=600_000)
    status_code: int | None = Field(default=None, ge=100, le=599)
    ts: datetime | None = None
    trace_id: str | None = Field(default=None, max_length=64)
    attrs: dict[str, Any] = Field(default_factory=dict)

    @field_validator("level", mode="before")
    @classmethod
    def _upper(cls, v):
        return v.upper() if isinstance(v, str) else v

    @property
    def is_error(self) -> bool:
        return self.level in (Level.ERROR, Level.CRITICAL) or (
            self.status_code is not None and self.status_code >= 500
        )


class EventBatch(BaseModel):
    events: list[EventIn] = Field(min_length=1)
