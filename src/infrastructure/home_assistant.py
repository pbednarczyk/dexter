import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from math import isfinite
from typing import AsyncIterator

import httpx
from pydantic import BaseModel, ConfigDict, Field

from src.application.tools import ToolRegistry
from src.domain.tools import ToolError
from src.infrastructure.config import Settings
from src.infrastructure.fake_home import FakeHomeState, create_registry
from src.infrastructure.ha_registries import EntityMetadata, HomeAssistantRegistries

ENTITY_ID_PATTERN = r"^[a-z0-9_]+\.[a-z0-9_]+$"
MAX_ENTITY_ID_LENGTH = 255


class ListEntitiesArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)
    query: str | None = Field(default=None, max_length=255, description="Substring of name or entity ID. Omit or pass an empty string to skip.")
    domain: str | None = Field(default=None, pattern=r"^[a-z0-9_]+$", max_length=255)
    state: str | None = Field(default=None, min_length=1, max_length=255, description="Exact current state, e.g. on, off or unavailable.")
    area: str | None = Field(default=None, min_length=1, max_length=255, description="Exact area name or ID, case-insensitive.")
    label: str | None = Field(default=None, min_length=1, max_length=255, description="Exact label name or ID, case-insensitive. Includes inherited labels.")
    limit: int = Field(default=20, ge=1, le=50)


class GetEntityStateArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    entity_id: str = Field(pattern=ENTITY_ID_PATTERN, max_length=MAX_ENTITY_ID_LENGTH)


class HomeAssistantClient:
    def __init__(self, client: httpx.AsyncClient):
        self._client = client

    async def list_states(self) -> list:
        return await self._get("/api/states", list)

    async def get_state(self, entity_id: str) -> dict:
        if len(entity_id) > MAX_ENTITY_ID_LENGTH or not re.fullmatch(ENTITY_ID_PATTERN, entity_id):
            raise ToolError("Invalid Home Assistant entity ID")
        state = await self._get(f"/api/states/{entity_id}", dict, entity_lookup=True)
        if state.get("entity_id") != entity_id:
            raise ToolError("Invalid Home Assistant response")
        return state

    async def _get(self, path: str, payload_type: type, *, entity_lookup: bool = False):
        try:
            response = await self._client.get(path, follow_redirects=False)
        except httpx.TimeoutException:
            raise ToolError("Home Assistant request timed out") from None
        except (httpx.RequestError, httpx.InvalidURL):
            raise ToolError("Could not communicate with Home Assistant") from None

        if response.status_code in (401, 403):
            raise ToolError("Home Assistant authorization failed")
        if response.status_code == 404 and entity_lookup:
            raise ToolError("Home Assistant entity not found")
        if not response.is_success:
            raise ToolError(f"Home Assistant returned HTTP {response.status_code}")

        try:
            payload = response.json()
        except ValueError:
            raise ToolError("Invalid Home Assistant response") from None
        if not isinstance(payload, payload_type):
            raise ToolError("Invalid Home Assistant response")
        return payload


def _attributes(state: dict) -> dict:
    attributes = state.get("attributes")
    return attributes if isinstance(attributes, dict) else {}


def _summary(state: dict) -> dict:
    entity_id = state["entity_id"]
    name = _attributes(state).get("friendly_name")
    return {
        "entity_id": entity_id,
        "name": name[:255] if isinstance(name, str) and name else entity_id,
        "state": state["state"][:255],
    }


def _search_text(value: str) -> str:
    return value.casefold().replace("_", " ")


@dataclass
class ListEntities:
    client: HomeAssistantClient
    registries: HomeAssistantRegistries
    name = "list_entities"
    description = (
        "Find Home Assistant entities. Combine optional filters query, domain, state, area and label. "
        "For lights that are on use domain=light and state=on with no query. "
        "Returns entities, total_count, truncated and metadata_available."
    )
    arguments_model = ListEntitiesArguments

    async def execute(self, arguments: ListEntitiesArguments) -> dict:
        try:
            metadata = await self.registries.get()
        except ToolError:
            if arguments.area or arguments.label:
                raise
            metadata = None

        query = _search_text(arguments.query or "")
        empty_metadata = EntityMetadata()
        matches = []
        total_count = 0
        for state in await self.client.list_states():
            if not isinstance(state, dict) or not isinstance(state.get("state"), str):
                continue
            entity_id = state.get("entity_id")
            if (not isinstance(entity_id, str) or len(entity_id) > MAX_ENTITY_ID_LENGTH
                    or not re.fullmatch(ENTITY_ID_PATTERN, entity_id)):
                continue
            if arguments.domain and entity_id.split(".", 1)[0] != arguments.domain:
                continue
            if arguments.state is not None and state["state"] != arguments.state:
                continue
            summary = _summary(state)
            if query not in _search_text(summary["name"]) and query not in _search_text(entity_id):
                continue
            details = metadata.get(entity_id, empty_metadata) if metadata is not None else empty_metadata
            if arguments.area and (details.area is None or not details.area.matches(arguments.area)):
                continue
            if arguments.label and not any(label.matches(arguments.label) for label in details.labels):
                continue
            total_count += 1
            if len(matches) < arguments.limit:
                matches.append({
                    **summary,
                    "area": details.area.as_result() if details.area else None,
                    "labels": [label.as_result() for label in details.labels],
                })
        return {
            "entities": matches,
            "total_count": total_count,
            "truncated": total_count > len(matches),
            "metadata_available": metadata is not None,
        }


@dataclass
class GetEntityState:
    client: HomeAssistantClient
    name = "get_entity_state"
    description = "Read the current state and selected attributes of an exact Home Assistant entity ID."
    arguments_model = GetEntityStateArguments

    async def execute(self, arguments: GetEntityStateArguments) -> dict:
        state = await self.client.get_state(arguments.entity_id)
        if not isinstance(state.get("state"), str):
            raise ToolError("Invalid Home Assistant response")
        attributes = _attributes(state)
        selected = {}
        for key in ("brightness", "temperature", "current_temperature", "color_temp_kelvin"):
            value = attributes.get(key)
            if type(value) is int or (type(value) is float and isfinite(value)):
                selected[key] = value
        unit = attributes.get("unit_of_measurement")
        if isinstance(unit, str):
            selected["unit_of_measurement"] = unit[:32]
        return {**_summary(state), "attributes": selected}


@asynccontextmanager
async def create_home_registry(settings: Settings) -> AsyncIterator[ToolRegistry]:
    if settings.home_backend == "fake":
        yield create_registry(FakeHomeState())
    else:
        registries = HomeAssistantRegistries(
            httpx.URL(str(settings.ha_url)), settings.ha_token, settings.ha_timeout_seconds,
            settings.ha_registry_cache_ttl_seconds,
        )
        async with httpx.AsyncClient(
            base_url=str(settings.ha_url),
            headers={"Authorization": f"Bearer {settings.ha_token.get_secret_value()}"},
            timeout=settings.ha_timeout_seconds,
            trust_env=False,
        ) as http_client:
            client = HomeAssistantClient(http_client)
            yield ToolRegistry([ListEntities(client, registries), GetEntityState(client)])
