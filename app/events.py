"""Structured progress events."""

from dataclasses import dataclass
from typing import Literal

Status = Literal["start", "ok", "fail", "progress", "info"]


@dataclass(frozen=True, slots=True)
class Event:
    """One thing that happened during an install."""

    step: str
    status: Status
    detail: str = ""
    done: int = 0
    total: int = 0
