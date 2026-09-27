from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field


class ToolCall(BaseModel):
    name: str
    arguments: Any = Field(default_factory=dict)


class Message(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_name: str | None = None


class ProviderError(Exception):
    """Unavailable provider or invalid provider response."""


class LLMProvider(Protocol):
    async def chat(self, messages: list[Message], tools: list[dict]) -> Message: ...
