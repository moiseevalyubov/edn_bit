from pydantic import Field
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

    model_config = {"env_file": ".env"}


settings = Settings()
