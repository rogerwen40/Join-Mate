from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ActivityRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    activity_type: str
    starts_at: datetime
    location: str
    min_people: int
    max_people: int
    fee: int
    description: str
    status: str
    created_at: datetime
