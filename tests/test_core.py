import asyncio
import json
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient

from src.application.agent import AgentEngine
from src.domain.llm import Message, ProviderError, ToolCall
from src.domain.tools import ToolError
from src.infrastructure.api import create_app
from src.infrastructure.config import Settings
from src.infrastructure.fake_home import FakeHomeState, create_registry
from src.infrastructure.ollama import OllamaLLMProvider
from tests.mock_llm import MockLLMProvider


def run(awaitable):
    return asyncio.run(awaitable)


def call(name='get_temperature', arguments=None):
    return Message(role='assistant', tool_calls=[ToolCall(
        name=name, arguments={'room': 'attic'} if arguments is None else arguments)])


def test_unknown_tool():
    with pytest.raises(ToolError, match='Unknown'):
        run(create_registry(FakeHomeState()).execute('exec', {'command': 'ls'}))


@pytest.mark.parametrize('arguments', [{}, {'room': 1}, {'room': 'unknown'},
                                      {'room': 'attic', 'extra': True}, 'attic'])
def test_invalid_arguments(arguments):
    state = FakeHomeState()
    with pytest.raises(ToolError, match='Invalid'):
        run(create_registry(state).execute('turn_on_light', arguments))
    assert state.lights['attic'] == 'off'


def test_fake_home():
    registry = create_registry(FakeHomeState())
    assert run(registry.execute('get_temperature', {'room': 'attic'})) == 21.7
    assert run(registry.execute('get_light_state', {'room': 'attic'})) == 'off'
    assert run(registry.execute('turn_on_light', {'room': 'attic'})) == 'success'
    assert run(registry.execute('get_light_state', {'room': 'attic'})) == 'on'
    run(registry.execute('turn_off_light', {'room': 'attic'}))
    assert run(registry.execute('get_light_state', {'room': 'attic'})) == 'off'


def test_health_and_chat():
    provider = MockLLMProvider([call(), Message(role='assistant', content='Na strychu jest 21.7°C.')])
    with TestClient(create_app(AgentEngine(provider, create_registry(FakeHomeState())))) as client:
        health = client.get('/health')
        assert health.status_code == 200
        assert health.json() == {'status': 'ok'}
        response = client.post('/api/chat', json={'message': 'Jaka jest temperatura na strychu?'})
        assert response.status_code == 200
        body = response.json()
        assert str(UUID(body['trace_id'])) == response.headers['X-Trace-ID']
        assert body['trace_id'] != health.headers['X-Trace-ID']
        assert body['response'] == 'Na strychu jest 21.7°C.'
        assert body['actions'][0]['result'] == 21.7
        assert provider.requests[1][0][-1].role == 'tool'
        assert json.loads(provider.requests[1][0][-1].content) == {'result': 21.7}
        assert client.post('/api/chat', json={'message': '  '}).status_code == 422


@pytest.mark.parametrize('tool_call', [call('unknown'), call(arguments={'room': 5})])
def test_invalid_call_returns_to_model(tool_call):
    provider = MockLLMProvider([tool_call, Message(role='assistant', content='Nie udało się.')])
    result = run(AgentEngine(provider, create_registry(FakeHomeState())).run('test', 'trace'))
    assert result.actions[0].error
    assert 'error' in json.loads(provider.requests[1][0][-1].content)


def test_step_limit():
    provider = MockLLMProvider([call()] * 5)
    result = run(AgentEngine(provider, create_registry(FakeHomeState())).run('test', 'trace'))
    assert len(provider.requests) == 5
    assert len(result.actions) == 5
    assert 'limit' in result.response


def test_batch_limit():
    batch = Message(role='assistant', tool_calls=call().tool_calls * 20)
    provider = MockLLMProvider([batch])
    result = run(AgentEngine(provider, create_registry(FakeHomeState())).run('test', 'trace'))
    assert len(result.actions) == 5


def test_provider_failure_keeps_actions():
    class FailingProvider(MockLLMProvider):
        async def chat(self, messages, tools):
            if self.requests:
                raise ProviderError('offline')
            return await super().chat(messages, tools)

    provider = FailingProvider([call('turn_on_light')])
    result = run(AgentEngine(provider, create_registry(FakeHomeState())).run('test', 'trace'))
    assert result.actions[0].result == 'success'
    assert 'niedostępny' in result.response


def test_ollama_wire_protocol():
    def handler(request):
        body = json.loads(request.content)
        assert request.url.path == '/api/chat'
        assert body['model'] == 'test-model'
        assert body['stream'] is False
        assert body['messages'][-1]['tool_name'] == 'get_temperature'
        assert body['messages'][0]['tool_calls'][0]['function']['name'] == 'get_temperature'
        assert len(body['tools']) == 4
        return httpx.Response(200, json={'message': {
            'role': 'assistant', 'content': '', 'tool_calls': [
                {'function': {'name': 'get_light_state', 'arguments': {'room': 'attic'}}}]}})

    async def scenario():
        async with httpx.AsyncClient(base_url='http://ollama', transport=httpx.MockTransport(handler)) as client:
            reply = await OllamaLLMProvider(client, 'test-model').chat([
                call(), Message(role='tool', tool_name='get_temperature', content='21.7')],
                create_registry(FakeHomeState()).schemas())
            assert reply.tool_calls[0].name == 'get_light_state'
    run(scenario())


@pytest.mark.parametrize('payload', [{}, {'message': {'role': 'user'}},
                                     {'message': {'role': 'assistant', 'tool_calls': None}},
                                     {'message': {'role': 'assistant', 'content': ''}}])
def test_malformed_ollama(payload):
    async def scenario():
        async with httpx.AsyncClient(base_url='http://ollama', transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=payload))) as client:
            with pytest.raises(ProviderError):
                await OllamaLLMProvider(client, 'test').chat([], [])
    run(scenario())


def test_config_rejects_excessive_steps():
    with pytest.raises(ValueError):
        Settings(_env_file=None, ollama_url='http://localhost:11434', ollama_model='test', agent_max_steps=6)
