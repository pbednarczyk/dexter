"""Native Ollama message format shared by HTTP and worker transports."""
from src.domain.llm import Message, ToolCall


def messages_to_wire(messages: list[Message]) -> list[dict]:
    result = []
    for message in messages:
        item = {"role": message.role, "content": message.content}
        if message.tool_calls:
            item["tool_calls"] = [{"function": call.model_dump()} for call in message.tool_calls]
        if message.tool_name:
            item["tool_name"] = message.tool_name
        result.append(item)
    return result


def assistant_from_wire(raw: dict) -> Message:
    if not isinstance(raw, dict) or raw.get("role") != "assistant":
        raise ValueError("Expected assistant message")
    calls = [ToolCall.model_validate(call["function"]) for call in raw.get("tool_calls", [])]
    reply = Message(role="assistant", content=raw.get("content", ""), tool_calls=calls)
    if not reply.tool_calls and not reply.content.strip():
        raise ValueError("Empty response")
    return reply
