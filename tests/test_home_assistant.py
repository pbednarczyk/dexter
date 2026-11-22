import asyncio
import json
from contextlib import asynccontextmanager
from functools import partial

import httpx
import pytest
from fastapi.testclient import TestClient

from src.domain.llm import Message, ToolCall
from src.domain.tools import ToolError
from src.infrastructure import api, ha_registries
from src.infrastructure.config import Settings
from src.infrastructure.home_assistant import create_home_registry
from tests.mock_llm import MockLLMProvider


def settings(**overrides):
    values = {
        "ollama_url": "http://ollama",
        "ollama_model": "test",
        "home_backend": "ha",
        "ha_url": "http://home-assistant:8123",
        "ha_token": "test-token",
    }
    return Settings(_env_file=None, **(values | overrides))


def mock_transport(monkeypatch, handler):
    def checked_handler(request):
        assert request.method == "GET"
        assert request.url.host == "home-assistant"
        assert request.headers["Authorization"] == "Bearer test-token"
        return handler(request)

    monkeypatch.setattr(httpx, "AsyncClient", partial(
        httpx.AsyncClient, transport=httpx.MockTransport(checked_handler),
    ))


def mock_websocket(monkeypatch, snapshots=None, *, auth_type="auth_ok", reject=None):
    snapshots = snapshots or {}
    connections = []
    commands = [
        "config/entity_registry/list", "config/device_registry/list",
        "config/area_registry/list", "config/label_registry/list",
    ]

    class Socket:
        def __init__(self):
            self.sent = []

        async def send(self, data):
            message = json.loads(data)
            index = len(self.sent)
            expected = (
                {"type": "auth", "access_token": "test-token"} if index == 0
                else {"id": index, "type": commands[index - 1]}
            )
            assert message == expected
            self.sent.append(message)

        async def recv(self):
            index = len(self.sent)
            if index < 2:
                return json.dumps({"type": "auth_required" if not index else auth_type})
            command = self.sent[-1]["type"]
            return json.dumps({
                "type": "result", "id": index - 1, "success": command != reject,
                "result": snapshots.get(command, []), "error": {"message": "test-token"},
            })

    @asynccontextmanager
    async def connect(url, **options):
        assert url == "ws://home-assistant:8123/api/websocket"
        assert options["proxy"] is None
        assert options["logger"].disabled
        socket = Socket()
        connections.append(socket)
        yield socket

    monkeypatch.setattr(ha_registries, "_ConnectWithoutRedirects", connect)
    return connections


def entity(entity_id, state, **attributes):
    return {"entity_id": entity_id, "state": state, "attributes": attributes}


def test_list_entities(monkeypatch):
    mock_websocket(monkeypatch)
    states = [
        entity("light.attic", "on", friendly_name="Światło na strychu", private="hidden"),
        entity("switch.strych_swiatlo", "off"),
        entity("sensor.attic_temperature", "21.5"),
        *[entity(f"light.lamp_{i}", "off") for i in range(60)],
    ]

    def handler(request):
        assert request.url.path == "/api/states"
        assert not request.url.query
        return httpx.Response(200, json=states)

    mock_transport(monkeypatch, handler)

    async def scenario():
        async with create_home_registry(settings()) as registry:
            async def find(**arguments):
                return (await registry.execute("list_entities", arguments))["entities"]

            assert await find(query="STRYCH", domain="light") == [
                {"entity_id": "light.attic", "name": "Światło na strychu", "state": "on", "area": None, "labels": []},
            ]
            assert await find(query="strych swiatlo") == [
                {"entity_id": "switch.strych_swiatlo", "name": "switch.strych_swiatlo", "state": "off", "area": None, "labels": []},
            ]
            assert len(await find(query="attic")) == 2
            assert await find(domain="binary_sensor") == []
            assert len(await find()) == 20
            assert len(await find(domain="light", limit=50)) == 50
            assert len(await find(limit=1)) == 1
            result = await registry.execute("list_entities", {"query": "  ", "limit": 50})
            assert len(result["entities"]) == 50
            assert result["total_count"] == 63
            assert result["truncated"] is True
            result = await registry.execute("list_entities", {"query": "", "state": "on"})
            assert result["total_count"] == 1
            assert result["truncated"] is False
            assert result["metadata_available"] is True

    asyncio.run(scenario())


@pytest.mark.parametrize(("arguments", "detail"), [
    ({"limit": 51}, "limit: Input should be less than or equal to 50"),
    ({"limit": 0}, "limit: Input should be greater than or equal to 1"),
    ({"limit": "test-token"}, "limit: Input should be a valid integer"),
    ({"test-token": "attic"}, "arguments: Extra inputs are not permitted"),
])
def test_invalid_list_arguments(monkeypatch, arguments, detail):
    mock_transport(monkeypatch, lambda request: pytest.fail("unexpected HTTP request"))

    async def scenario():
        async with create_home_registry(settings()) as registry:
            with pytest.raises(ToolError, match=detail) as exc:
                await registry.execute("list_entities", arguments)
            assert "test-token" not in str(exc.value)

    asyncio.run(scenario())


@pytest.mark.parametrize("entity_id", ["../services", "light.foo/bar", "https://example.com", "light.foo?x=1"])
def test_invalid_entity_id(monkeypatch, entity_id):
    mock_transport(monkeypatch, lambda request: pytest.fail("unexpected HTTP request"))

    async def scenario():
        async with create_home_registry(settings()) as registry:
            with pytest.raises(ToolError, match="Invalid tool arguments"):
                await registry.execute("get_entity_state", {"entity_id": entity_id})

    asyncio.run(scenario())


@pytest.mark.parametrize(("status", "error"), [
    (404, "entity not found"),
    (401, "authorization failed"),
    (403, "authorization failed"),
    (302, "HTTP 302"),
    (503, "HTTP 503"),
    (None, "timed out"),
])
def test_state_errors(monkeypatch, status, error):
    def handler(request):
        assert request.url.path == "/api/states/light.attic"
        assert request.extensions["timeout"]["read"] == 0.5
        if status is None:
            raise httpx.ReadTimeout("mock timeout", request=request)
        return httpx.Response(status, headers={"Location": "http://elsewhere/"}, text="test-token")

    mock_transport(monkeypatch, handler)

    async def scenario():
        async with create_home_registry(settings(ha_timeout_seconds=0.5)) as registry:
            with pytest.raises(ToolError, match=error) as exc:
                await registry.execute("get_entity_state", {"entity_id": "light.attic"})
            assert "test-token" not in str(exc.value)

    asyncio.run(scenario())


@pytest.mark.parametrize("missing", [{"ha_token": ""}, {"ha_url": None}])
def test_ha_requires_configuration(missing):
    with pytest.raises(ValueError, match="is required"):
        settings(**missing)


def test_api_ha_mode(monkeypatch, caplog):
    def handler(request):
        assert request.url.path == "/api/states/light.strych_swiatlo"
        return httpx.Response(200, json=entity(
            "light.strych_swiatlo", "on", friendly_name="Strych", brightness=230,
            current_temperature=21.5, unit_of_measurement="°C", access_token="private",
        ))

    mock_transport(monkeypatch, handler)
    provider = MockLLMProvider([
        Message(role="assistant", tool_calls=[
            ToolCall(name="get_entity_state", arguments={"entity_id": "light.strych_swiatlo"}),
            ToolCall(name="list_entities", arguments={"limit": 51}),
            ToolCall(name="turn_off_light", arguments={"room": "attic"}),
            ToolCall(name="turn_on_light", arguments={"room": "attic"}),
            ToolCall(name="call_service", arguments={"domain": "light", "service": "turn_off"}),
        ]),
        Message(role="assistant", content="Światło jest włączone."),
    ])

    @asynccontextmanager
    async def create_provider(_settings):
        yield provider

    monkeypatch.setattr(api, "create_provider", create_provider)
    monkeypatch.setattr(api, "Settings", settings)
    with TestClient(api.create_app()) as client:
        response = client.post("/api/chat", json={"message": "Czy światło na strychu jest włączone?"})

    assert response.status_code == 200
    actions = response.json()["actions"]
    assert actions[0]["result"] == {
        "entity_id": "light.strych_swiatlo", "name": "Strych", "state": "on",
        "attributes": {"brightness": 230, "current_temperature": 21.5, "unit_of_measurement": "°C"},
    }
    assert actions[0]["error"] is None
    assert "limit: Input should be less than or equal to 50" in actions[1]["error"]
    assert [action["error"] for action in actions[2:]] == ["Unknown tool"] * 3
    tool_messages = [msg for msg in provider.requests[1][0] if msg.role == "tool"]
    assert json.loads(tool_messages[1].content) == {"error": actions[1]["error"]}
    schema = provider.requests[0][1][0]["function"]["parameters"]
    assert schema.get("required", []) == []
    assert schema["additionalProperties"] is False
    assert schema["properties"]["limit"]["maximum"] == 50
    assert "minLength" not in schema["properties"]["query"]["anyOf"][0]
    assert [tool["function"]["name"] for tool in provider.requests[0][1]] == [
        "list_entities", "get_entity_state",
    ]
    assert "test-token" not in response.text + caplog.text + repr(provider.requests)
    assert "access_token" not in response.text + caplog.text + repr(provider.requests)


def test_registry_filters_and_cache(monkeypatch):
    snapshots = {
        "config/entity_registry/list": [
            {"entity_id": "light.ceiling", "device_id": "bridge"},
            {"entity_id": "light.desk", "device_id": "bridge", "area_id": "study",
             "labels": ["reading", "shared"]},
            {"entity_id": "sensor.temperature", "device_id": "bridge"},
        ],
        "config/device_registry/list": [
            {"id": "bridge", "area_id": "attic", "labels": ["shared"]},
        ],
        "config/area_registry/list": [
            {"area_id": "attic", "name": "Strych", "labels": ["den"]},
            {"area_id": "study", "name": "Gabinet", "labels": ["office"]},
        ],
        "config/label_registry/list": [
            {"label_id": "den", "name": "Jaskinia"},
            {"label_id": "shared", "name": "Wspólna"},
            {"label_id": "reading", "name": "Do czytania"},
            {"label_id": "office", "name": "Praca"},
        ],
    }
    connections = mock_websocket(monkeypatch, snapshots)
    states = [
        entity("light.ceiling", "on"),
        entity("light.desk", "off"),
        entity("sensor.temperature", "21.5"),
        entity("light.unregistered", "on"),
    ]
    mock_transport(monkeypatch, lambda request: httpx.Response(200, json=states))
    now = 100.0
    monkeypatch.setattr(ha_registries, "monotonic", lambda: now)

    async def scenario():
        nonlocal now
        async with create_home_registry(settings(ha_registry_cache_ttl_seconds=10)) as registry:
            async def find(**arguments):
                return (await registry.execute("list_entities", arguments))["entities"]

            ceiling = {
                "entity_id": "light.ceiling", "name": "light.ceiling", "state": "on",
                "area": {"id": "attic", "name": "Strych"},
                "labels": [{"id": "den", "name": "Jaskinia"}, {"id": "shared", "name": "Wspólna"}],
            }
            both = await asyncio.gather(
                find(domain="light", state="on", area="STRYCH", label="jaskinia", query="CEILING"),
                find(domain="light", area="attic", label="den"),
            )
            assert both == [[ceiling], [ceiling]]
            assert len(connections) == 1
            assert len(connections[0].sent) == 5
            [desk] = await find(state="off", area="gabinet", label="Do czytania")
            assert desk["area"] == {"id": "study", "name": "Gabinet"}
            assert [label["id"] for label in desk["labels"]] == ["office", "reading", "shared"]
            assert await find(area="study", label="den") == []
            assert len(await find(label="Wspólna")) == 3
            assert [item["entity_id"] for item in await find(area="Strych", domain="sensor")] == [
                "sensor.temperature",
            ]
            assert len(await find(domain="light", state="on")) == 2

            states[0]["state"] = "off"
            snapshots["config/area_registry/list"][0]["name"] = "Poddasze"
            assert await find(domain="light", state="on", area="Strych") == []
            assert len(await find(domain="light", state="off", area="Strych")) == 1
            assert len(connections) == 1
            now += 10
            assert await find(area="Strych") == []
            assert len(await find(area="Poddasze")) == 2
            assert len(connections) == 2

    asyncio.run(scenario())


@pytest.mark.parametrize(("auth_type", "reject", "error"), [
    ("auth_invalid", None, "authorization failed"),
    ("auth_ok", "config/label_registry/list", "rejected the config/label_registry/list command"),
])
def test_registry_failure_preserves_state_only_search(monkeypatch, auth_type, reject, error):
    mock_websocket(monkeypatch, auth_type=auth_type, reject=reject)
    mock_transport(monkeypatch, lambda request: httpx.Response(200, json=[entity("light.attic", "on")]))

    async def scenario():
        async with create_home_registry(settings()) as registry:
            with pytest.raises(ToolError, match=error) as exc:
                await registry.execute("list_entities", {"area": "attic"})
            assert "test-token" not in str(exc.value)
            result = await registry.execute("list_entities", {"state": "on"})
            assert result["metadata_available"] is False
            assert result["total_count"] == 1

    asyncio.run(scenario())
