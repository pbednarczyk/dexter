import json
import logging
from typing import Any

from pydantic import BaseModel, Field

from src.application.tools import ToolRegistry
from src.domain.llm import LLMProvider, Message, ProviderError
from src.domain.tools import ToolError

logger = logging.getLogger(__name__)


class Action(BaseModel):
    tool: str
    arguments: Any
    result: Any = None
    error: str | None = None


class ChatResult(BaseModel):
    trace_id: str
    response: str
    actions: list[Action] = Field(default_factory=list)


class AgentEngine:
    def __init__(self, provider: LLMProvider, registry: ToolRegistry, max_steps: int = 5):
        if not 1 <= max_steps <= 5:
            raise ValueError("max_steps must be between 1 and 5")
        self.provider, self.registry, self.max_steps = provider, registry, max_steps

    async def run(self, text: str, trace_id: str) -> ChatResult:
        messages = [Message(role="system", content=(
            "Jesteś DEXTER, lokalny asystent domu. Odpowiadaj po polsku. "
            "Stan domu sprawdzaj wyłącznie przez dostępne narzędzia. "
            "Strych to room=attic. Nie wymyślaj wyników ani sukcesu operacji. "
            "Błąd narzędzia oznacza, że operacja nie została potwierdzona."
        )), Message(role="user", content=text)]
        actions = []
        for _ in range(self.max_steps):
            try:
                reply = await self.provider.chat(messages, self.registry.schemas())
            except ProviderError:
                return ChatResult(trace_id=trace_id, actions=actions,
                                  response="Model jest niedostępny lub zwrócił błędną odpowiedź. Spróbuj ponownie.")
            messages.append(reply)
            if not reply.tool_calls:
                return ChatResult(trace_id=trace_id, response=reply.content, actions=actions)
            # Bound tool executions too, even if the model returns a large batch.
            for call in reply.tool_calls:
                if len(actions) >= self.max_steps:
                    return self._limit(trace_id, actions)
                action = Action(tool=call.name, arguments=call.arguments)
                try:
                    action.result = await self.registry.execute(call.name, call.arguments)
                except ToolError as exc:
                    action.error = str(exc)
                except Exception:
                    logger.exception("Tool failed trace_id=%s tool=%s", trace_id, call.name)
                    action.error = "Tool execution failed"
                actions.append(action)
                logger.info("tool_action trace_id=%s action=%s", trace_id, action.model_dump_json())
                payload = {"error": action.error} if action.error else {"result": action.result}
                messages.append(Message(role="tool", tool_name=call.name,
                                        content=json.dumps(payload, ensure_ascii=False)))
        return self._limit(trace_id, actions)

    @staticmethod
    def _limit(trace_id, actions):
        return ChatResult(trace_id=trace_id, actions=actions,
                          response="Osiągnięto limit kroków agenta. Sprawdź wykonane akcje.")
