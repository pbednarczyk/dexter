import httpx
from pydantic import ValidationError

from src.domain.llm import Message, ProviderError, ToolCall


class OllamaLLMProvider:
    def __init__(self, client: httpx.AsyncClient, model: str):
        self.client, self.model = client, model

    async def chat(self, messages: list[Message], tools: list[dict]) -> Message:
        wire_messages = []
        for message in messages:
            item = {"role": message.role, "content": message.content}
            if message.tool_calls:
                item["tool_calls"] = [{"function": call.model_dump()} for call in message.tool_calls]
            if message.tool_name:
                item["tool_name"] = message.tool_name
            wire_messages.append(item)
        try:
            response = await self.client.post("/api/chat", json={
                "model": self.model, "messages": wire_messages, "tools": tools, "stream": False,
            })
            response.raise_for_status()
            raw = response.json()["message"]
            if raw.get("role") != "assistant":
                raise ValueError("Expected assistant message")
            calls = [ToolCall.model_validate(call["function"]) for call in raw.get("tool_calls", [])]
            reply = Message(role="assistant", content=raw.get("content", ""), tool_calls=calls)
            if not reply.tool_calls and not reply.content.strip():
                raise ValueError("Empty response")
            return reply
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError, ValidationError) as exc:
            raise ProviderError("Ollama request failed") from exc
