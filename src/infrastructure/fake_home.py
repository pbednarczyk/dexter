from dataclasses import dataclass, field
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict

from src.application.tools import ToolRegistry


class RoomArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    room: Literal["attic"]


@dataclass
class FakeHomeState:
    temperatures: dict[str, float] = field(default_factory=lambda: {"attic": 21.7})
    lights: dict[str, str] = field(default_factory=lambda: {"attic": "off"})

    def set_light(self, room: str, state: str) -> str:
        self.lights[room] = state
        return "success"


@dataclass
class FakeHomeTool:
    name: str
    description: str
    handler: Callable
    arguments_model: type[BaseModel] = RoomArguments

    async def execute(self, arguments: RoomArguments):
        return self.handler(arguments.room)


def create_registry(state: FakeHomeState) -> ToolRegistry:
    return ToolRegistry([
        FakeHomeTool("get_temperature", "Get room temperature in Celsius", lambda room: state.temperatures[room]),
        FakeHomeTool("get_light_state", "Get light state: on or off", lambda room: state.lights[room]),
        FakeHomeTool("turn_on_light", "Turn room light on", lambda room: state.set_light(room, "on")),
        FakeHomeTool("turn_off_light", "Turn room light off", lambda room: state.set_light(room, "off")),
    ])
