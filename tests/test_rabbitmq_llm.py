import asyncio
import json
from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import aio_pika
import httpx
import pytest
from fastapi.testclient import TestClient

from src.application.agent import AgentEngine
from src.domain.llm import Message, ProviderError, ToolCall
from src.infrastructure.api import create_app
from src.infrastructure.config import Settings
from src.infrastructure.fake_home import FakeHomeState, create_registry
from src.infrastructure.ollama import OllamaLLMProvider
from src.infrastructure.providers import create_provider
from src.infrastructure.rabbitmq_llm import RabbitMQLLMProvider, parse_result


@pytest.fixture(autouse=True)
def isolated_settings_environment(monkeypatch):
    # Compose injects .env into the test container; config tests own their ENV.
    for field in Settings.model_fields:
        monkeypatch.delenv(field.upper(), raising=False)


JOB_ID = '17793513-fc3a-4228-a8d1-7e419d4ce5c1'
TOOL_MESSAGE = {'role': 'assistant', 'content': '', 'tool_calls': [
    {'function': {'name': 'get_temperature', 'arguments': {'room': 'attic'}}}]}


def settings(**overrides):
    return Settings(_env_file=None, **{
        'llm_provider': 'rabbitmq', 'ollama_model': 'qwen3:14b',
        'rabbitmq_host': 'broker', 'rabbitmq_password': 'test-only-password',
        'rabbitmq_reply_to_enabled': True, **overrides,
    })


def envelope(job_id=JOB_ID, message=None):
    return {'schema_version': 1, 'job_id': job_id, 'type': 'llm.chat.result',
            'status': 'completed', 'model': 'qwen3:14b',
            'message': message or deepcopy(TOOL_MESSAGE), 'error': None,
            'worker': 'SilverMonkey', 'completed_at': '2026-09-27T00:00:00Z'}


class Broker:
    """Fake only the AMQP boundary; execute the real provider and AgentEngine."""
    def __init__(self, monkeypatch, transform=None, silent=False):
        self.published = []
        self.connections = []
        self.incoming = []
        self.transform = transform
        self.silent = silent
        self.connect = AsyncMock(side_effect=self.open)
        monkeypatch.setattr('src.infrastructure.rabbitmq_llm.aio_pika.connect', self.connect)

    async def open(self, **kwargs):
        callback = None
        queue = SimpleNamespace(name=f'amq.gen-private-{len(self.connections)}')

        async def consume(handler, **kwargs):
            nonlocal callback
            callback = handler
        queue.consume = AsyncMock(side_effect=consume)

        async def publish(message, **kwargs):
            self.published.append((message, kwargs))
            if self.silent:
                return
            job = json.loads(message.body)
            result = envelope(job['job_id'])
            if self.transform:
                result = self.transform(result, job)
            incoming = SimpleNamespace(body=json.dumps(result).encode(),
                                       correlation_id=job['job_id'], type='llm.chat.result',
                                       ack=AsyncMock(), reject=AsyncMock())
            self.incoming.append(incoming)
            await callback(incoming)

        channel = SimpleNamespace(set_qos=AsyncMock(), declare_queue=AsyncMock(return_value=queue),
                                  default_exchange=SimpleNamespace(publish=AsyncMock(side_effect=publish)))
        connection = SimpleNamespace(channel=AsyncMock(return_value=channel), close=AsyncMock(),
                                     test_channel=channel, test_queue=queue)
        self.connections.append(connection)
        return connection


def test_provider_selection_from_env(monkeypatch):
    monkeypatch.setenv('OLLAMA_MODEL', 'qwen3:14b')
    monkeypatch.setenv('OLLAMA_URL', 'http://ollama:11434')
    monkeypatch.setenv('RABBITMQ_HOST', 'broker')
    monkeypatch.setenv('RABBITMQ_PASSWORD', 'test-only-password')
    connect = AsyncMock(side_effect=AssertionError('No connection at startup'))
    monkeypatch.setattr('src.infrastructure.rabbitmq_llm.aio_pika.connect', connect)

    async def check():
        for name, cls in [('ollama', OllamaLLMProvider), ('rabbitmq', RabbitMQLLMProvider)]:
            monkeypatch.setenv('LLM_PROVIDER', name)
            async with create_provider(Settings(_env_file=None)) as provider:
                assert isinstance(provider, cls)
    asyncio.run(check())
    connect.assert_not_called()


def test_job_properties_native_messages_and_private_queue(monkeypatch):
    broker = Broker(monkeypatch)
    tools = create_registry(FakeHomeState()).schemas()
    messages = [Message(role='user', content='Jaka temperatura?'),
                Message(role='assistant', tool_calls=[ToolCall(name='get_temperature', arguments={'room':'attic'})]),
                Message(role='tool', content='{"result": 21.7}', tool_name='get_temperature')]
    reply = asyncio.run(RabbitMQLLMProvider(settings(llm_options={'temperature': 0})).chat(messages, tools))
    assert reply.tool_calls[0].arguments == {'room': 'attic'}
    assert len(broker.published) == 1
    wire, publish = broker.published[0]
    job = json.loads(wire.body)
    assert job['schema_version'] == 1
    assert job['type'] == 'llm.chat'
    assert UUID(job['job_id']).version == 4
    assert job['model'] == 'qwen3:14b'
    assert job['tools'] == tools
    assert job['options'] == {'temperature': 0}
    assert datetime.fromisoformat(job['created_at']).utcoffset().total_seconds() == 0
    assert job['messages'][1]['tool_calls'] == TOOL_MESSAGE['tool_calls']
    assert job['messages'][2] == {'role': 'tool', 'content': '{"result": 21.7}', 'tool_name': 'get_temperature'}
    assert wire.correlation_id == wire.message_id == job['job_id']
    assert wire.type == 'llm.chat'
    assert wire.content_type == 'application/json'
    assert wire.content_encoding == 'utf-8'
    assert wire.delivery_mode == 2
    assert wire.reply_to.startswith('amq.gen-private-')
    assert publish == {'routing_key': 'llm.jobs', 'mandatory': True}
    connection = broker.connections[0]
    connection.channel.assert_awaited_once_with(publisher_confirms=True, on_return_raises=True)
    connection.test_channel.declare_queue.assert_awaited_once_with('', exclusive=True, auto_delete=True, durable=False)
    assert broker.connect.call_args.kwargs['virtualhost'] == '/wordtracker'
    connection.close.assert_awaited_once()
    broker.incoming[0].ack.assert_awaited_once()


def test_same_domain_response_as_ollama():
    async def scenario():
        async with httpx.AsyncClient(base_url='http://ollama', transport=httpx.MockTransport(
                lambda req: httpx.Response(200, json={'message': TOOL_MESSAGE}))) as client:
            direct = await OllamaLLMProvider(client, 'qwen3:14b').chat([], [])
        remote = parse_result(json.dumps(envelope()).encode(), JOB_ID, 'llm.chat.result', JOB_ID)
        assert direct == remote
    asyncio.run(scenario())


@pytest.mark.parametrize('patch', [
    {'schema_version': 2}, {'schema_version': True}, {'schema_version': '1'},
    {'job_id': 'other'}, {'type': 'llm.other.result'}, {'status': 'pending'},
    {'message': None}, {'message': []}, {'message': {'role': 'user', 'content': 'x'}},
    {'message': {'role': 'assistant', 'tool_calls': None}},
    {'message': {'role': 'assistant', 'content': ''}},
    {'status': 'failed', 'message': None, 'error': None},
    {'status': 'failed', 'message': None, 'error': {'code': '', 'message': 'x'}},
    {'status': 'failed', 'message': {}, 'error': {'code': 'x', 'message': 'x'}},
    {'error': {'code': 'bad', 'message': 'inconsistent'}},
])
def test_malformed_result(patch):
    result = {**envelope(), **patch}
    with pytest.raises(ProviderError, match='Invalid RabbitMQ result'):
        parse_result(json.dumps(result).encode(), JOB_ID, 'llm.chat.result', JOB_ID)


@pytest.mark.parametrize('body', [b'not json', b'[]', b'null', b'\xff'])
def test_invalid_json(body):
    with pytest.raises(ProviderError):
        parse_result(body, JOB_ID, 'llm.chat.result', JOB_ID)


@pytest.mark.parametrize('correlation,message_type', [('other', 'llm.chat.result'),
                                                     (None, 'llm.chat.result'), (JOB_ID, 'llm.chat')])
def test_mismatched_properties(correlation, message_type):
    with pytest.raises(ProviderError):
        parse_result(json.dumps(envelope()).encode(), correlation, message_type, JOB_ID)


def test_failed_result_is_safe_and_not_requeued(monkeypatch):
    broker = Broker(monkeypatch, transform=lambda result, job: {
        **result, 'status': 'failed', 'message': None,
        'error': {'code': 'upstream', 'message': 'private-secret-password'}})
    with pytest.raises(ProviderError, match='RabbitMQ worker failed') as error:
        asyncio.run(RabbitMQLLMProvider(settings()).chat([], []))
    assert 'private-secret' not in str(error.value)
    broker.incoming[0].reject.assert_awaited_once_with(requeue=False)
    broker.incoming[0].ack.assert_not_called()
    broker.connections[0].close.assert_awaited_once()


def test_worker_without_reply_to_is_blocked_before_publish(monkeypatch):
    broker = Broker(monkeypatch)
    config = settings(rabbitmq_reply_to_enabled=False)
    with pytest.raises(ProviderError, match='reply_to'):
        asyncio.run(RabbitMQLLMProvider(config).chat([], []))
    broker.connect.assert_not_called()
    assert not broker.published


def test_old_worker_ignoring_reply_to_times_out_without_shared_consumer(monkeypatch):
    broker = Broker(monkeypatch, silent=True)
    with pytest.raises(ProviderError, match='timed out'):
        asyncio.run(RabbitMQLLMProvider(settings(rabbitmq_llm_timeout=0.02)).chat([], []))
    assert len(broker.published) == 1
    connection = broker.connections[0]
    connection.close.assert_awaited_once()
    connection.test_channel.declare_queue.assert_awaited_once_with('', exclusive=True, auto_delete=True, durable=False)


def test_connect_timeout_no_publish(monkeypatch):
    async def hang(**kwargs):
        await asyncio.Future()
    monkeypatch.setattr('src.infrastructure.rabbitmq_llm.aio_pika.connect', hang)
    with pytest.raises(ProviderError, match='timed out'):
        asyncio.run(RabbitMQLLMProvider(settings(rabbitmq_llm_timeout=0.01)).chat([], []))


def test_transport_failure_is_safe_and_not_retried(monkeypatch):
    connect = AsyncMock(side_effect=OSError('amqp://private-secret'))
    monkeypatch.setattr('src.infrastructure.rabbitmq_llm.aio_pika.connect', connect)
    with pytest.raises(ProviderError, match='transport failed') as error:
        asyncio.run(RabbitMQLLMProvider(settings()).chat([], []))
    assert 'private-secret' not in str(error.value)
    connect.assert_awaited_once()


def test_agent_engine_with_real_provider_mocked_transport(monkeypatch):
    def respond(result, job):
        if job['messages'][-1]['role'] == 'tool':
            assert json.loads(job['messages'][-1]['content']) == {'result': 21.7}
            result['message'] = {'role': 'assistant', 'content': 'Na strychu jest 21.7°C.'}
        return result
    broker = Broker(monkeypatch, transform=respond)
    engine = AgentEngine(RabbitMQLLMProvider(settings()), create_registry(FakeHomeState()))
    result = asyncio.run(engine.run('Jaka jest temperatura na strychu?', 'trace'))
    assert result.response == 'Na strychu jest 21.7°C.'
    assert result.actions[0].result == 21.7
    assert len(broker.published) == 2
    assert broker.published[0][0].reply_to != broker.published[1][0].reply_to
    assert broker.published[0][0].correlation_id != broker.published[1][0].correlation_id


def test_health_does_not_connect_and_chat_keeps_contract(monkeypatch):
    config = settings(rabbitmq_reply_to_enabled=False)
    monkeypatch.setattr('src.infrastructure.api.Settings', lambda: config)
    broker = Broker(monkeypatch)
    with TestClient(create_app()) as client:
        assert client.get('/health').json() == {'status': 'ok'}
        response = client.post('/api/chat', json={'message': 'test'})
        assert response.status_code == 200
        assert set(response.json()) == {'trace_id', 'response', 'actions'}
        assert response.json()['actions'] == []
        assert response.json()['trace_id'] == response.headers['x-trace-id']
    broker.connect.assert_not_called()


def test_config_defaults_and_provider_specific_validation():
    config = Settings(_env_file=None, llm_provider='rabbitmq', ollama_model='test',
                      rabbitmq_host='broker', rabbitmq_password='private-secret')
    assert config.ollama_url is None
    assert config.rabbitmq_reply_to_enabled is False
    assert 'private-secret' not in repr(config)
    with pytest.raises(ValueError):
        Settings(_env_file=None, ollama_model='test')
    for patch in [{'llm_provider':'unknown'}, {'rabbitmq_llm_timeout':0},
                  {'rabbitmq_llm_timeout':float('inf')}, {'rabbitmq_password':''},
                  {'rabbitmq_vhost':'/'}, {'rabbitmq_llm_jobs_queue':'other'}]:
        with pytest.raises(ValueError):
            settings(**patch)


@pytest.mark.parametrize('mode', ['timeout', 'nack'])
def test_publish_failure_closes_connection_without_retry(monkeypatch, mode):
    broker = Broker(monkeypatch)
    original_open = broker.open

    async def open_with_failure(**kwargs):
        connection = await original_open(**kwargs)

        async def fail(*args, **kwargs):
            if mode == 'timeout':
                await asyncio.Future()
            raise aio_pika.AMQPException('private-broker-detail')
        connection.test_channel.default_exchange.publish.side_effect = fail
        return connection
    broker.connect.side_effect = open_with_failure
    with pytest.raises(ProviderError, match='timed out' if mode == 'timeout' else 'transport failed'):
        asyncio.run(RabbitMQLLMProvider(settings(rabbitmq_llm_timeout=0.02)).chat([], []))
    broker.connections[0].test_channel.default_exchange.publish.assert_awaited_once()
    broker.connections[0].close.assert_awaited_once()


def test_cancel_cleans_private_connection(monkeypatch):
    broker = Broker(monkeypatch, silent=True)

    async def scenario():
        task = asyncio.create_task(RabbitMQLLMProvider(settings()).chat([], []))
        while not broker.published:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(scenario())
    assert len(broker.published) == 1
    broker.connections[0].close.assert_awaited_once()


def test_concurrent_requests_use_distinct_reply_queues(monkeypatch):
    broker = Broker(monkeypatch)
    provider = RabbitMQLLMProvider(settings())

    async def scenario():
        replies = await asyncio.gather(provider.chat([], []), provider.chat([], []))
        assert all(reply.tool_calls for reply in replies)
    asyncio.run(scenario())
    assert len({item.reply_to for item, _ in broker.published}) == 2
    assert len({item.correlation_id for item, _ in broker.published}) == 2
    assert all(connection.close.await_count == 1 for connection in broker.connections)
