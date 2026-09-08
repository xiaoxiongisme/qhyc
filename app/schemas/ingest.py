from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class IngestReportOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    task_id: int
    task_type: str
    label: str | None = None
    status: str
    started_at: datetime
    finished_at: datetime | None = None
    message: str | None = None