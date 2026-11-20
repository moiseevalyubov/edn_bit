from pydantic import Field, model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str
    secret_key: str = Field(repr=False)
    app_base_url: str = ""
    bitrix_client_id: str = ""
    bitrix_client_secret: str = ""
    # Test-only switch: when "1", outgoing sends to edna fail immediately with a
    # permanent error. Used to verify the "undelivered" operator notice (#2) on a
    # live portal without touching real API keys. Off by default; toggle in Render.
    edna_force_fail: str = ""
    # ВРЕМЕННО, проба 3 по #22: "1" включает адреса /probe22/* (app/routers/probe22.py).
    probe_22: str = ""
    # #22: working CRM outreach stays off until the staged implementation and
    # portal checks are complete. These are deployment controls, not UI steps.
    crm_outreach_enabled: bool = False
    # Temporary preprod comparison: only this channel serves outgoing Telegram
    # bytes through the existing MAX memory cache. Zero disables the probe.
    telegram_media_legacy_cache_channel_id: int = 0
    bitrix_expected_app_code: str = ""

    model_config = {"env_file": ".env"}

    @model_validator(mode="before")
    @classmethod
    def ignore_retired_telegram_switch(cls, values):
        # Existing .env files may still contain the retired switch. Accept it
        # without letting either 0 or 1 control Telegram availability.
        if isinstance(values, dict):
            values = dict(values)
            values.pop("telegram_enabled", None)
            values.pop("TELEGRAM_ENABLED", None)
        return values


settings = Settings()
