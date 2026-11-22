import asyncio
import json
import logging
from dataclasses import dataclass
from time import monotonic

import httpx
from pydantic import SecretStr
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from src.domain.tools import ToolError

# WebSocket debug logs include the authentication frame.
_logger = logging.Logger(__name__)
_logger.disabled = True

_REGISTRIES = (
    ("config/entity_registry/list", "entity_id"),
    ("config/device_registry/list", "id"),
    ("config/area_registry/list", "area_id"),
    ("config/label_registry/list", "label_id"),
)


class _ConnectWithoutRedirects(connect):
    def process_redirect(self, exc: Exception) -> Exception:
        return exc


@dataclass(frozen=True)
class RegistryName:
    id: str
    name: str

    def matches(self, value: str) -> bool:
        return value.casefold() in (self.id.casefold(), self.name.casefold())

    def as_result(self) -> dict[str, str]:
        return {"id": self.id, "name": self.name[:255]}


@dataclass(frozen=True)
class EntityMetadata:
    area: RegistryName | None = None
    labels: tuple[RegistryName, ...] = ()


def _label_ids(entry: dict) -> set[str]:
    labels = entry.get("labels")
    return {label for label in labels if isinstance(label, str)} if isinstance(labels, list) else set()


def _id(entry: dict, key: str) -> str:
    value = entry.get(key)
    return value if isinstance(value, str) else ""


def _name(entry: dict, entry_id: str) -> RegistryName:
    return RegistryName(entry_id, _id(entry, "name") or entry_id)


def _resolve_metadata(
    entities: dict[str, dict], devices: dict[str, dict],
    areas: dict[str, dict], labels: dict[str, dict],
) -> dict[str, EntityMetadata]:
    metadata = {}
    for entity_id, entity in entities.items():
        device = devices.get(_id(entity, "device_id"), {})
        area_id = _id(entity, "area_id") or _id(device, "area_id")
        area = areas.get(area_id, {})
        label_ids = _label_ids(entity) | _label_ids(device) | _label_ids(area)
        metadata[entity_id] = EntityMetadata(
            area=_name(area, area_id) if area_id else None,
            labels=tuple(_name(labels.get(label_id, {}), label_id) for label_id in sorted(label_ids)),
        )
    return metadata


async def _receive(socket) -> dict:
    message = json.loads(await socket.recv())
    if not isinstance(message, dict):
        raise ToolError("Invalid Home Assistant WebSocket response")
    return message


class HomeAssistantRegistries:
    def __init__(self, base_url: httpx.URL, token: SecretStr, timeout: float, ttl: float):
        scheme = "wss" if base_url.scheme == "https" else "ws"
        self._url = str(base_url.copy_with(
            scheme=scheme, path=base_url.path.rstrip("/") + "/api/websocket",
            query=None, fragment=None, username=None, password=None,
        ))
        self._token = token
        self._timeout = timeout
        self._ttl = ttl
        self._lock = asyncio.Lock()
        self._expires_at = 0.0
        self._metadata: dict[str, EntityMetadata] = {}

    async def get(self) -> dict[str, EntityMetadata]:
        async with self._lock:
            if monotonic() >= self._expires_at:
                self._metadata = await self._load()
                self._expires_at = monotonic() + self._ttl
            return self._metadata

    async def _load(self) -> dict[str, EntityMetadata]:
        snapshots = []
        try:
            async with asyncio.timeout(self._timeout), _ConnectWithoutRedirects(
                self._url, proxy=None, open_timeout=self._timeout, close_timeout=self._timeout,
                max_size=8 * 1024 * 1024, logger=_logger,
            ) as socket:
                if (await _receive(socket)).get("type") != "auth_required":
                    raise ToolError("Invalid Home Assistant WebSocket handshake")
                await socket.send(json.dumps({
                    "type": "auth", "access_token": self._token.get_secret_value(),
                }))
                auth_type = (await _receive(socket)).get("type")
                if auth_type == "auth_invalid":
                    raise ToolError("Home Assistant WebSocket authorization failed")
                if auth_type != "auth_ok":
                    raise ToolError("Invalid Home Assistant WebSocket handshake")

                for request_id, (command, key) in enumerate(_REGISTRIES, start=1):
                    await socket.send(json.dumps({"id": request_id, "type": command}))
                    response = await _receive(socket)
                    if response.get("id") != request_id or response.get("type") != "result":
                        raise ToolError("Invalid Home Assistant registry response")
                    if response.get("success") is not True:
                        raise ToolError(f"Home Assistant rejected the {command} command")
                    rows = response.get("result")
                    if not isinstance(rows, list) or any(
                        not isinstance(row, dict) or not _id(row, key) for row in rows
                    ):
                        raise ToolError("Invalid Home Assistant registry response")
                    snapshots.append({row[key]: row for row in rows})
        except TimeoutError:
            raise ToolError("Home Assistant WebSocket request timed out") from None
        except (OSError, WebSocketException):
            raise ToolError("Could not communicate with Home Assistant WebSocket") from None
        except ValueError:
            raise ToolError("Invalid Home Assistant WebSocket response") from None
        return _resolve_metadata(*snapshots)
