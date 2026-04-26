from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "sqlite:///./app.db"
    app_base_url: str = ""
    bitrix_client_id: str = ""
    bitrix_client_secret: str = ""

    model_config = {"env_file": ".env"}


settings = Settings()
