from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class TaskRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    task_type: str
    label: str | None = None
    status: str
    started_at: datetime
    finished_at: datetime | None = None
    message: str | None = None
    payload: dict | None = None