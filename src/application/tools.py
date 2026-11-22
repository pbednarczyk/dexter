from pydantic import ValidationError

from src.domain.tools import Tool, ToolError


class ToolRegistry:
    def __init__(self, tools: list[Tool]):
        self._tools = {tool.name: tool for tool in tools}
        if len(self._tools) != len(tools):
            raise ValueError("Duplicate tool name")

    def schemas(self) -> list[dict]:
        return [{"type": "function", "function": {
            "name": tool.name, "description": tool.description,
            "parameters": tool.arguments_model.model_json_schema(),
        }} for tool in self._tools.values()]

    async def execute(self, name: str, arguments):
        tool = self._tools.get(name)
        if tool is None:
            raise ToolError("Unknown tool")
        try:
            validated = tool.arguments_model.model_validate(arguments)
        except ValidationError as exc:
            details = []
            for error in exc.errors(include_input=False, include_context=False, include_url=False):
                location = error["loc"]
                field = location[0] if location and location[0] in tool.arguments_model.model_fields else "arguments"
                details.append(f"{field}: {error['msg']}")
            raise ToolError("Invalid tool arguments: " + "; ".join(details[:10])) from None
        return await tool.execute(validated)
