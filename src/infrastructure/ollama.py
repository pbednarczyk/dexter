import httpx

from src.domain.llm import Message, ProviderError
from src.infrastructure.llm_wire import assistant_from_wire, messages_to_wire


class OllamaLLMProvider:
    def __init__(self, client: httpx.AsyncClient, model: str, options: dict | None = None):
        self.client, self.model = client, model
        self.options = options or {}

    async def chat(self, messages: list[Message], tools: list[dict]) -> Message:
        try:
            response = await self.client.post("/api/chat", json={
                "model": self.model, "messages": messages_to_wire(messages),
                "tools": tools, "options": self.options, "stream": False,
            })
            response.raise_for_status()
            return assistant_from_wire(response.json()["message"])
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ProviderError("Ollama request failed") from exc
