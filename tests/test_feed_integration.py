"""Feed lifecycle against the real client and a strictly local synthetic Viking WS."""

import asyncio
import json
import time
from collections.abc import Callable
from typing import Any

import pytest
from websockets.asyncio.server import ServerConnection, serve

from app.config import Settings
from app.credentials import VikingCredentials
from app.portfolio_feed import PortfolioFeedManager
from app.viking_client import VikingAPIError, VikingClient

OWNER = ("integration-client", "synthetic-credential-fingerprint")
TS = 1780000000000000001


class SyntheticViking:
    def __init__(self) -> None:
        self.connections = 0
        self.requests: list[dict[str, Any]] = []
        self.pending: dict[str, tuple[ServerConnection, dict[str, Any]]] = {}
        self.active: dict[str, tuple[ServerConnection, dict[str, Any]]] = {}
        self.unsubscribes: list[str] = []
        self.premature_unsubscribes: list[str] = []
        self.changed = asyncio.Condition()

    async def handle(self, ws: ServerConnection) -> None:
        self.connections += 1
        try:
            async for raw in ws:
                if raw == "7":
                    await ws.send("7")
                    continue
                message = json.loads(raw)
                self.requests.append(message)
                kind = message["type"]
                if kind == "authorization_key":
                    await self.reply(ws, message, "p", {})
                elif kind == "portfolio.subscribe":
                    self.pending[message["eid"]] = (ws, message)
                    if message["data"]["p_id"] == "legacy":
                        await self.snapshot(message)
                elif kind == "portfolio.unsubscribe":
                    target = message["data"]["sub_eid"]
                    self.unsubscribes.append(target)
                    if target in self.pending:
                        self.premature_unsubscribes.append(target)
                    self.active.pop(target, None)
                    await self.reply(ws, message, "p", {})
                else:
                    raise AssertionError(f"Unexpected synthetic WS message: {kind}")
                async with self.changed:
                    self.changed.notify_all()
        finally:
            for registry in (self.active, self.pending):
                for eid, (connection, _) in list(registry.items()):
                    if connection is ws:
                        registry.pop(eid)

    @staticmethod
    async def reply(
        ws: ServerConnection, request: dict[str, Any], result: str, data: dict[str, Any],
    ) -> None:
        await ws.send(json.dumps({
            "type": request["type"], "eid": request["eid"], "ts": TS, "r": result, "data": data,
        }))

    async def subscribed(self, portfolio: str) -> dict[str, Any]:
        def matching() -> list[dict[str, Any]]:
            return [message for message in self.requests if (
                message["type"] == "portfolio.subscribe" and message["data"]["p_id"] == portfolio
            )]

        async with asyncio.timeout(2), self.changed:
            await self.changed.wait_for(matching)
            return matching()[0]

    async def snapshot(self, request: dict[str, Any]) -> None:
        ws, _ = self.pending.pop(request["eid"])
        self.active[request["eid"]] = (ws, request)
        await self.reply(ws, request, "s", {
            **request["data"],
            "value": {"name": request["data"]["p_id"], "securities": {}, "pos": 0},
        })

    async def deny(self, request: dict[str, Any]) -> None:
        ws, _ = self.pending.pop(request["eid"])
        await self.reply(ws, request, "e", {"msg": "Synthetic permission denied", "code": 403})

    async def update(self, subscription_id: str, position: int) -> None:
        ws, request = self.active[subscription_id]
        await self.reply(ws, request, "u", {
            **request["data"], "value": {"name": request["data"]["p_id"], "pos": position},
        })


@pytest.fixture
async def connected_feed():
    upstream = SyntheticViking()
    async with serve(upstream.handle, "127.0.0.1", 0, ping_interval=None) as server:
        port = server.sockets[0].getsockname()[1]
        settings = Settings(
            _env_file=None, viking_ws_url=f"ws://127.0.0.1:{port}",
            viking_request_timeout_seconds=10,
        )
        credentials = VikingCredentials("fixture@example.invalid", "synthetic-key", "synthetic-role")
        client = VikingClient(settings, credentials)
        manager = PortfolioFeedManager(reconnect_delay=0)
        try:
            yield upstream, client, manager
        finally:
            await manager.close()
            await client.close()


async def eventually(predicate: Callable[[], bool]) -> None:
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.001)


async def assert_legacy_update(
    upstream: SyntheticViking, client: VikingClient, subscription_id: str, position: int,
) -> None:
    assert client.connected and upstream.connections == 1
    await upstream.update(subscription_id, position)
    result = await client.get_portfolio_updates(subscription_id, wait_seconds=2)
    assert result["active"]
    assert [event["value"]["pos"] for event in result["events"]] == [position]


async def test_failed_feed_keeps_legacy_and_cleans_slow_subscription_only_after_late_snapshot(connected_feed):
    upstream, client, manager = connected_feed
    legacy = await client.subscribe_portfolio(robot_id="1", portfolio="legacy")
    legacy_id = legacy["subscription_id"]
    original_ws = client._ws
    opening = asyncio.create_task(manager.open(
        OWNER, client, [("1", "slow"), ("1", "denied")], expires_at=time.time() + 300,
    ))
    slow = await upstream.subscribed("slow")
    denied = await upstream.subscribed("denied")
    await upstream.deny(denied)

    # Failure must not wait for the unrelated snapshot or the 10-second client timeout.
    with pytest.raises(VikingAPIError) as caught:
        await asyncio.wait_for(opening, 2)
    assert caught.value.code == 403
    assert not manager._hubs and not manager._feeds and not manager._pending
    assert client._ws is original_ws
    assert set(client._subscriptions) == {legacy_id}
    assert set(client._abandoned_subscriptions) == {slow["eid"]}
    assert set(client._pending) == {slow["eid"]}
    assert not client._subscription_starts
    assert slow["eid"] in upstream.pending
    assert not upstream.unsubscribes
    await assert_legacy_update(upstream, client, legacy_id, 17)
    assert not upstream.unsubscribes

    await upstream.snapshot(slow)
    await eventually(lambda: not client._abandoned_subscriptions and not client._pending)
    await eventually(lambda: not client._subscription_cleanup_tasks)
    assert upstream.unsubscribes == [slow["eid"]]
    assert not upstream.premature_unsubscribes and not upstream.pending
    assert set(upstream.active) == set(client._subscriptions) == {legacy_id}
    assert client._ws is original_ws
    await assert_legacy_update(upstream, client, legacy_id, 18)


async def test_cancelled_feed_waiter_preserves_real_shared_subscription_and_legacy(connected_feed):
    upstream, client, manager = connected_feed
    legacy = await client.subscribe_portfolio(robot_id="1", portfolio="legacy")
    legacy_id = legacy["subscription_id"]
    original_ws = client._ws

    async def open_feed() -> dict[str, Any]:
        return await manager.open(OWNER, client, [("1", "slow")], expires_at=time.time() + 300)

    first = asyncio.create_task(open_feed())
    slow = await upstream.subscribed("slow")
    second = asyncio.create_task(open_feed())
    await eventually(lambda: len(manager._pending) == 2)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert not second.done()
    assert not client._abandoned_subscriptions and not upstream.unsubscribes
    assert slow["eid"] in client._subscription_starts
    assert sum(message["type"] == "portfolio.subscribe" for message in upstream.requests) == 2
    await assert_legacy_update(upstream, client, legacy_id, 23)

    await upstream.snapshot(slow)
    feed = await asyncio.wait_for(second, 2)
    assert feed["items"][0]["portfolio"] == "slow"
    assert set(upstream.active) == {legacy_id, slow["eid"]}
    await upstream.update(slow["eid"], 24)
    cursors = [{key: item[key] for key in ("stream_id", "epoch", "seq")} for item in feed["items"]]
    result = await manager.read(OWNER, feed["feed_id"], cursors, wait_seconds=2)
    assert result["items"][0]["value"]["pos"] == 24

    await manager.close_feed(OWNER, feed["feed_id"])
    assert upstream.unsubscribes == [slow["eid"]]
    assert not upstream.premature_unsubscribes
    assert not manager._hubs and not manager._pending and not manager._feeds
    assert not client._pending and not client._abandoned_subscriptions
    assert set(upstream.active) == set(client._subscriptions) == {legacy_id}
    assert client._ws is original_ws
    await assert_legacy_update(upstream, client, legacy_id, 25)
