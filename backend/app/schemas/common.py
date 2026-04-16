from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class WebSocketEvent(BaseModel):
    event: str
    timestamp: datetime
    payload: dict

