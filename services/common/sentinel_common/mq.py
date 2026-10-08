"""RabbitMQ topology + helpers (topic exchange, durable queues, dead-letter queue)."""
import asyncio
import json
import logging
from typing import Any

import aio_pika
from aio_pika.abc import AbstractExchange, AbstractQueue, AbstractRobustConnection

log = logging.getLogger(__name__)

EXCHANGE = "sentinel.events"
DLX = "sentinel.dlx"
DEAD_QUEUE = "sentinel.dead"
Q_RAW = "events.raw"
Q_INCIDENTS = "incidents.detected"
RK_RAW = "event.raw"
RK_INCIDENT = "incident.detected"


async def connect(url: str, attempts: int = 60, delay: float = 2.0) -> AbstractRobustConnection:
    for attempt in range(1, attempts + 1):
        try:
            return await aio_pika.connect_robust(url)
        except Exception as exc:  # noqa: BLE001
            log.warning("rabbitmq not ready", extra={"attempt": attempt, "error": str(exc)})
            await asyncio.sleep(delay)
    raise RuntimeError("could not connect to RabbitMQ")


async def declare_topology(channel) -> tuple[AbstractExchange, dict[str, AbstractQueue]]:
    exchange = await channel.declare_exchange(EXCHANGE, aio_pika.ExchangeType.TOPIC, durable=True)
    dlx = await channel.declare_exchange(DLX, aio_pika.ExchangeType.FANOUT, durable=True)
    dead = await channel.declare_queue(DEAD_QUEUE, durable=True)
    await dead.bind(dlx)

    args = {"x-dead-letter-exchange": DLX}
    queues: dict[str, AbstractQueue] = {}
    for name, key in ((Q_RAW, RK_RAW), (Q_INCIDENTS, RK_INCIDENT)):
        queue = await channel.declare_queue(name, durable=True, arguments=args)
        await queue.bind(exchange, routing_key=key)
        queues[name] = queue
    return exchange, queues


async def publish_json(exchange: AbstractExchange, routing_key: str, payload: Any) -> None:
    message = aio_pika.Message(
        body=json.dumps(payload, default=str).encode(),
        content_type="application/json",
        delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
    )
    await exchange.publish(message, routing_key=routing_key)
