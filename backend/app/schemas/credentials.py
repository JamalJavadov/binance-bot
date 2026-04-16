from datetime import datetime

from pydantic import BaseModel


class CredentialsPayload(BaseModel):
    api_key: str
    public_key_pem: str
    private_key_pem: str


class CredentialsStatus(BaseModel):
    has_credentials: bool
    last_updated: datetime | None = None
    masked_api_key: str | None = None


class ConnectionTestResponse(BaseModel):
    success: bool
    balance_usdt: float | None = None
    message: str

