from typing import Literal

from pydantic import AnyHttpUrl, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)
    app_env: str = "dev"
    llm_provider: Literal["ollama", "rabbitmq"] = "ollama"
    llm_options: dict = Field(default_factory=dict)
    ollama_url: AnyHttpUrl | None = None
    ollama_model: str = Field(min_length=1)
    agent_max_steps: int = Field(default=5, ge=1, le=5)
    home_backend: Literal["fake", "ha"] = "fake"
    ha_url: AnyHttpUrl | None = None
    ha_token: SecretStr = SecretStr("")
    ha_timeout_seconds: float = Field(default=5, gt=0, allow_inf_nan=False)
    ollama_timeout_seconds: float = Field(default=120, gt=0, allow_inf_nan=False)
    rabbitmq_host: str | None = None
    rabbitmq_port: int = Field(default=5672, ge=1, le=65535)
    rabbitmq_vhost: Literal["/wordtracker"] = "/wordtracker"
    rabbitmq_user: str = Field(default="wordtracker", min_length=1)
    rabbitmq_password: SecretStr = SecretStr("")
    rabbitmq_llm_jobs_queue: Literal["llm.jobs"] = "llm.jobs"
    rabbitmq_llm_timeout: float = Field(default=300, gt=0, allow_inf_nan=False)
    # Operator confirmation, not automatic worker capability negotiation.
    rabbitmq_reply_to_enabled: bool = False

    @model_validator(mode="after")
    def validate_home_backend(self):
        if self.home_backend == "ha":
            if self.ha_url is None:
                raise ValueError("HA_URL is required for the HA backend")
            if not self.ha_token.get_secret_value().strip():
                raise ValueError("HA_TOKEN is required for the HA backend")
        return self

    @model_validator(mode="after")
    def validate_provider(self):
        if self.llm_provider == "ollama" and self.ollama_url is None:
            raise ValueError("OLLAMA_URL is required for ollama")
        if self.llm_provider == "rabbitmq":
            if not self.rabbitmq_host or not self.rabbitmq_host.strip():
                raise ValueError("RABBITMQ_HOST is required for rabbitmq")
            if not self.rabbitmq_password.get_secret_value():
                raise ValueError("RABBITMQ_PASSWORD is required for rabbitmq")
        return self
