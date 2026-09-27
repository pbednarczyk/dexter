"""RPC-like transport. Never consumes or declares the shared llm.results queue.

Requires a worker that routes responses to AMQP reply_to. Disabled by default
until the operator confirms that worker capability.
"""
import asyncio
import json
from contextlib import suppress
from datetime import datetime, timezone
from uuid import uuid4

import aio_pika

from src.domain.llm import Message, ProviderError
from src.infrastructure.config import Settings
from src.infrastructure.llm_wire import assistant_from_wire, messages_to_wire


def parse_result(body: bytes, correlation_id: str | None, message_type: str | None,
                 job_id: str) -> Message:
    try:
        result = json.loads(body)
        if not isinstance(result, dict):
            raise ValueError("Expected result object")
        if (type(result.get("schema_version")) is not int or result["schema_version"] != 1
                or result.get("job_id") != job_id or correlation_id != job_id
                or result.get("type") != "llm.chat.result" or message_type != "llm.chat.result"):
            raise ValueError("Invalid result envelope")
        if result.get("status") == "failed":
            error = result.get("error")
            if ("message" not in result or result["message"] is not None
                    or not isinstance(error, dict)
                    or not all(isinstance(error.get(key), str) and error[key].strip()
                               for key in ("code", "message"))):
                raise ValueError("Invalid failed result")
            # Worker details may contain credentials or private upstream data.
            raise ProviderError("RabbitMQ worker failed")
        if result.get("status") != "completed" or result.get("error") is not None:
            raise ValueError("Invalid completed result")
        return assistant_from_wire(result["message"])
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ProviderError("Invalid RabbitMQ result") from exc


class RabbitMQLLMProvider:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def chat(self, messages: list[Message], tools: list[dict]) -> Message:
        if not self.settings.rabbitmq_reply_to_enabled:
            raise ProviderError("RabbitMQ RPC requires worker reply_to support")
        job_id = str(uuid4())
        job = {
            "schema_version": 1, "job_id": job_id, "type": "llm.chat",
            "model": self.settings.ollama_model, "messages": messages_to_wire(messages),
            "tools": tools, "options": self.settings.llm_options,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            # One deadline covers connect, declare, publish confirms and response.
            async with asyncio.timeout(self.settings.rabbitmq_llm_timeout):
                return await self._rpc(job)
        except TimeoutError:
            raise ProviderError("RabbitMQ LLM request timed out") from None
        except (aio_pika.AMQPException, OSError):
            raise ProviderError("RabbitMQ LLM transport failed") from None

    async def _rpc(self, job: dict) -> Message:
        connection = None
        try:
            # No reconnect/retry: a lost confirmation must not duplicate a job.
            connection = await aio_pika.connect(
                host=self.settings.rabbitmq_host, port=self.settings.rabbitmq_port,
                login=self.settings.rabbitmq_user,
                password=self.settings.rabbitmq_password.get_secret_value(),
                virtualhost=self.settings.rabbitmq_vhost,
                timeout=self.settings.rabbitmq_llm_timeout,
            )
            channel = await connection.channel(publisher_confirms=True, on_return_raises=True)
            await channel.set_qos(prefetch_count=1)
            queue = await channel.declare_queue("", exclusive=True, auto_delete=True, durable=False)
            received = asyncio.get_running_loop().create_future()

            async def on_result(incoming):
                if not received.done():
                    received.set_result(incoming)

            await queue.consume(on_result, no_ack=False)
            await channel.default_exchange.publish(
                aio_pika.Message(
                    body=json.dumps(job, ensure_ascii=False).encode("utf-8"),
                    content_type="application/json", content_encoding="utf-8",
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                    correlation_id=job["job_id"], message_id=job["job_id"],
                    type="llm.chat", reply_to=queue.name,
                ),
                routing_key=self.settings.rabbitmq_llm_jobs_queue, mandatory=True,
            )
            incoming = await received
            try:
                result = parse_result(incoming.body, incoming.correlation_id, incoming.type, job["job_id"])
            except ProviderError:
                await incoming.reject(requeue=False)
                raise
            await incoming.ack()
            return result
        finally:
            if connection is not None:
                # Closing deletes the exclusive queue, including on timeout/cancel.
                # Cleanup is bounded separately and never masks the original error.
                with suppress(Exception):
                    await asyncio.wait_for(connection.close(), timeout=5)
