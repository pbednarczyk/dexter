import asyncio
from contextlib import asynccontextmanager
from functools import partial

import httpx
import pytest
from fastapi.testclient import TestClient

from src.domain.llm import Message, ToolCall
from src.domain.tools import ToolError
from src.infrastructure import api
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


def entity(entity_id, state, **attributes):
    return {"entity_id": entity_id, "state": state, "attributes": attributes}


def test_list_entities(monkeypatch):
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
                return await registry.execute("list_entities", arguments)

            assert await find(query="STRYCH", domain="light") == [
                {"entity_id": "light.attic", "name": "Światło na strychu", "state": "on"},
            ]
            assert await find(query="strych swiatlo") == [
                {"entity_id": "switch.strych_swiatlo", "name": "switch.strych_swiatlo", "state": "off"},
            ]
            assert len(await find(query="attic")) == 2
            assert await find(domain="binary_sensor") == []
            assert len(await find()) == 20
            assert len(await find(domain="light", limit=50)) == 50
            assert len(await find(limit=1)) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("arguments", [{"limit": 51}, {"limit": 0}, {"room": "attic"}])
def test_invalid_list_arguments(monkeypatch, arguments):
    mock_transport(monkeypatch, lambda request: pytest.fail("unexpected HTTP request"))

    async def scenario():
        async with create_home_registry(settings()) as registry:
            with pytest.raises(ToolError, match="Invalid tool arguments"):
                await registry.execute("list_entities", arguments)

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
            ToolCall(name="turn_off_light", arguments={"room": "attic"}),
            ToolCall(name="turn_on_light", arguments={"room": "attic"}),
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
    assert [action["error"] for action in actions] == [None, "Unknown tool", "Unknown tool"]
    assert [tool["function"]["name"] for tool in provider.requests[0][1]] == [
        "list_entities", "get_entity_state",
    ]
    assert "test-token" not in response.text + caplog.text + repr(provider.requests)
    assert "access_token" not in response.text + caplog.text + repr(provider.requests)
