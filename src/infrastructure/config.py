from pydantic import AnyHttpUrl, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    app_env: str = "dev"
    ollama_url: AnyHttpUrl
    ollama_model: str = Field(min_length=1)
    agent_max_steps: int = Field(default=5, ge=1, le=5)
    ollama_timeout_seconds: float = Field(default=120, gt=0)
