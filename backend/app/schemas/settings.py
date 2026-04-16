from pydantic import BaseModel


class SettingsResponse(BaseModel):
    settings: dict[str, str]


class SettingsPatch(BaseModel):
    values: dict[str, str]

