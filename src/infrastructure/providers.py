import logging
from contextlib import asynccontextmanager

import httpx

from src.infrastructure.config import Settings
from src.infrastructure.ollama import OllamaLLMProvider
from src.infrastructure.rabbitmq_llm import RabbitMQLLMProvider


@asynccontextmanager
async def create_provider(settings: Settings):
    if settings.llm_provider == "rabbitmq":
        if not settings.rabbitmq_reply_to_enabled:
            logging.getLogger(__name__).warning(
                "RabbitMQ RPC disabled: worker reply_to support must be confirmed")
        # No broker connection during startup: /health remains process liveness.
        yield RabbitMQLLMProvider(settings)
    else:
        async with httpx.AsyncClient(base_url=str(settings.ollama_url),
                                     timeout=settings.ollama_timeout_seconds) as client:
            yield OllamaLLMProvider(client, settings.ollama_model, settings.llm_options)
