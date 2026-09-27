from typing import Literal

from pydantic import AnyHttpUrl, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    app_env: str = "dev"
    llm_provider: Literal["ollama", "rabbitmq"] = "ollama"
    llm_options: dict = Field(default_factory=dict)
    ollama_url: AnyHttpUrl | None = None
    ollama_model: str = Field(min_length=1)
    agent_max_steps: int = Field(default=5, ge=1, le=5)
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
    def validate_provider(self):
        if self.llm_provider == "ollama" and self.ollama_url is None:
            raise ValueError("OLLAMA_URL is required for ollama")
        if self.llm_provider == "rabbitmq":
            if not self.rabbitmq_host or not self.rabbitmq_host.strip():
                raise ValueError("RABBITMQ_HOST is required for rabbitmq")
            if not self.rabbitmq_password.get_secret_value():
                raise ValueError("RABBITMQ_PASSWORD is required for rabbitmq")
        return self
