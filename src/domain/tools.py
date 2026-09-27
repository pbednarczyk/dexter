from typing import Any, Protocol

from pydantic import BaseModel


class ToolError(Exception):
    pass


class Tool(Protocol):
    name: str
    description: str
    arguments_model: type[BaseModel]

    async def execute(self, arguments: BaseModel) -> Any: ...
