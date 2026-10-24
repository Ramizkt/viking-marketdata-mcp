"""Real client, synthetic local Viking peer; no production credentials or source calls."""

from __future__ import annotations

import asyncio
import json
from time import monotonic

import pytest
from websockets.asyncio.server import serve

from app.config import Settings
from app.credentials import VikingCredentials
from app.portfolio_control import WriteNotSent, WriteOutcomeUnknown, send_control_once
from app.viking_client import VikingClient, VikingRequestNotSent


class Peer:
    def __init__(self):
        self.sockets = []
        self.frames = []
        self.requests = []
        self.withheld = []
        self.hold_portfolios = set()
        self.hold_authorization = False
        self.hold_writes = False
        self.ack_heartbeat = True
        self.bad_unsubscribe = False

    async def handle(self, ws):
        generation = len(self.sockets)
        self.sockets.append(ws)
        async for raw in ws:
            self.frames.append((generation, monotonic(), raw))
            if raw == "7":
                if self.ack_heartbeat:
                    await ws.send("7")
                continue
            payload = json.loads(raw)
            for request in payload if isinstance(payload, list) else [payload]:
                self.requests.append((generation, request))
                hold = (
                    request["type"] == "authorization_key" and self.hold_authorization
                    or request["type"] == "portfolio.subscribe"
                    and request["data"]["p_id"] in self.hold_portfolios
                    or request["type"] == "portfolio.update" and self.hold_writes
                )
                if hold:
                    self.withheld.append((ws, request))
                else:
                    await self.reply(ws, request)

    async def reply(self, ws, request, *, result=None):
        kind, data = request["type"], request["data"]
        if kind == "portfolio.subscribe":
            result = result or "s"
            data = {**data, "value": {"name": data["p_id"], "securities": {}, "fin_res": 1}}
        elif kind == "portfolio.update":
            data = {"r_id": data["r_id"], "p_id": data["portfolio"]["name"]}
        if kind.endswith(".unsubscribe") and self.bad_unsubscribe:
            result = "u"
        await ws.send(json.dumps({
            "type": kind, "eid": request["eid"], "ts": 1,
            "r": result or "p", "data": data,
        }))

    def of_type(self, kind):
        return [request for _, request in self.requests if request["type"] == kind]


async def until(predicate, timeout=3):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.001)


@pytest.fixture
async def local_client(tmp_path):
    peer = Peer()
    async with serve(peer.handle, "127.0.0.1", 0, ping_interval=None) as server:
        port = server.sockets[0].getsockname()[1]
        client = VikingClient(
            Settings(
                _env_file=None, export_dir=tmp_path, viking_ws_url=f"ws://127.0.0.1:{port}",
                viking_request_timeout_seconds=2,
            ),
            VikingCredentials("fixture@example.invalid", "fixture-only", "trader"),
        )
        try:
            yield client, peer
        finally:
            await client.close()
            await asyncio.sleep(0)


async def test_default_budget_100_open_close_open_and_mass_reconnect(local_client):
    client, peer = local_client
    legacy = await client.subscribe_portfolio(robot_id="1", portfolio="legacy")

    async def open_hundred():
        return await asyncio.gather(*(
            client.subscribe_portfolio(robot_id="1", portfolio=f"p{i}") for i in range(100)
        ))

    started = monotonic()
    async with asyncio.timeout(20):
        first = await open_hundred()
        await asyncio.gather(*(client.unsubscribe_portfolio(item["subscription_id"]) for item in first))
        second = await open_hundred()
    assert monotonic() - started >= 10.5  # Production 10000-point/11-second limiter, no scaled clock.
    assert len(second) == 100 and len(peer.sockets) == 1
    assert len([raw for _, _, raw in peer.frames if raw == "7"]) >= 3
    await peer.reply(peer.sockets[0], peer.of_type("portfolio.subscribe")[0], result="u")
    updates = await client.get_portfolio_updates(legacy["subscription_id"], wait_seconds=1)
    assert updates["event_count"] == 1 and updates["events"][0]["portfolio"] == "legacy"
    assert client.connected and len(peer.sockets) == 1

    await peer.sockets[0].close()
    await until(lambda: not client.connected)
    third = await open_hundred()
    assert len(third) == 100 and len(peer.sockets) == 2
    assert set(client._subscriptions) == {item["subscription_id"] for item in third}
    assert len(peer.of_type("authorization_key")) == 2
    for index, (generation, stamp, _) in enumerate(peer.frames):
        window = [raw for gen, at, raw in peer.frames[:index + 1] if gen == generation and at > stamp - 10]
        assert sum(1 if raw == "7" else 49 for raw in window) <= 10_000


async def test_group_of_fifty_costs_one_frame(local_client):
    client, peer = local_client
    responses = await client._grouped_exchange(
        [("fixture.read", {}) for _ in range(150)], register_subscriptions=False,
    )
    assert len(responses) == 150
    assert len(peer.frames) == 4  # Authorization plus three groups.
    assert client._outbound_window.points == 4 * 49


@pytest.mark.parametrize("operation", ["subscription", "write"])
async def test_cancel_while_waiting_for_budget_does_not_send_or_close(local_client, operation):
    client, peer = local_client
    legacy = await client.subscribe_portfolio(robot_id="1", portfolio="legacy")
    entered = asyncio.Event()

    async def blocked_sleep(_):
        entered.set()
        await asyncio.Event().wait()

    client._outbound_window.record(monotonic(), 9990)
    client._outbound_sleep = blocked_sleep
    task = asyncio.create_task(
        client.subscribe_portfolio(robot_id="1", portfolio="new") if operation == "subscription"
        else send_control_once(client, robot_id="1", portfolio="legacy", action="stop")
    )
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert client.connected
    assert legacy["subscription_id"] in client._subscriptions
    assert not client._abandoned_subscriptions
    assert not peer.of_type("portfolio.update")
    assert len(peer.of_type("portfolio.subscribe")) == 1


async def test_admission_failure_is_not_sent_and_never_closes_or_retries(local_client):
    client, peer = local_client
    await client.authenticate()
    client._outbound_window.record(monotonic(), 9990)

    async def failed_admission(_):
        raise ConnectionError("synthetic admission failure")

    client._outbound_sleep = failed_admission
    with pytest.raises(WriteNotSent):
        await send_control_once(client, robot_id="1", portfolio="p", action="stop")
    assert client.connected and not peer.of_type("portfolio.update")


async def test_sent_write_timeout_remains_unknown_without_replay(local_client):
    client, peer = local_client
    await client.authenticate()
    peer.hold_writes = True
    client.settings.viking_request_timeout_seconds = 0.03
    with pytest.raises(WriteOutcomeUnknown):
        await send_control_once(client, robot_id="1", portfolio="p", action="stop")
    assert len(peer.of_type("portfolio.update")) == 1
    assert not client.connected


async def test_cancel_in_send_shields_frame_and_holds_lock_until_complete(local_client, monkeypatch):
    client, peer = local_client
    await client.authenticate()
    ws = client._ws
    original_send = ws.send
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_send(payload):
        await original_send(payload)
        if "portfolio.subscribe" in payload:
            entered.set()
            await release.wait()

    monkeypatch.setattr(ws, "send", slow_send)
    task = asyncio.create_task(client.subscribe_portfolio(robot_id="1", portfolio="cancelled"))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await until(lambda: bool(client._abandoned_subscriptions))
    tombstone = next(iter(client._abandoned_subscriptions.values()))
    assert not tombstone.send.task.done()
    assert client._send_lock.locked()
    assert not peer.of_type("portfolio.unsubscribe")
    release.set()
    await until(lambda: not client._abandoned_subscriptions)
    assert len(peer.of_type("portfolio.unsubscribe")) == 1
    assert client.connected and not client._send_lock.locked()


async def test_timeout_retains_late_ack_cleanup_and_malformed_unsubscribe_tombstone(local_client):
    client, peer = local_client
    peer.hold_portfolios.add("late")
    peer.bad_unsubscribe = True
    with pytest.raises(TimeoutError):
        await client._subscribe("portfolio.subscribe", {"r_id": "1", "p_id": "late"}, timeout=0.01)
    assert len(client._abandoned_subscriptions) == 1
    assert not peer.of_type("portfolio.unsubscribe")
    await asyncio.sleep(0.03)
    ws, request = peer.withheld.pop()
    await peer.reply(ws, request)
    await until(lambda: bool(peer.of_type("portfolio.unsubscribe")))
    await until(lambda: not client._subscription_cleanup_tasks)
    assert request["eid"] in client._abandoned_subscriptions
    assert client.connected


async def test_unanswered_cancelled_starts_are_bounded_and_release_on_generation_end(local_client):
    client, peer = local_client
    peer.hold_portfolios.add("never")
    clock = [monotonic()]
    client._outbound_clock = lambda: clock[0]

    async def advance(delay):
        clock[0] += delay + 0.001
        await asyncio.sleep(0)

    client._outbound_sleep = advance
    tasks = [
        asyncio.create_task(client.subscribe_portfolio(robot_id="1", portfolio="never"))
        for _ in range(256)
    ]
    await until(lambda: len(peer.of_type("portfolio.subscribe")) == 256)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    assert len(client._abandoned_subscriptions) == 256
    with pytest.raises(VikingRequestNotSent, match="not sent"):
        await client.subscribe_portfolio(robot_id="1", portfolio="overflow")
    assert len(peer.of_type("portfolio.subscribe")) == 256
    assert client.connected
    await client.close()
    peer.hold_portfolios.clear()
    await client.subscribe_portfolio(robot_id="1", portfolio="after-reconnect")
    assert client.connected and not client._abandoned_subscriptions


async def test_cancelled_cold_authorization_is_never_visible_as_connected(local_client):
    client, peer = local_client
    peer.hold_authorization = True
    cold = asyncio.create_task(client.subscribe_portfolio(robot_id="1", portfolio="first"))
    await until(lambda: bool(peer.of_type("authorization_key")))
    assert client._ws is not None and not client.connected
    second = asyncio.create_task(client.subscribe_portfolio(robot_id="1", portfolio="second"))
    await asyncio.sleep(0.02)
    assert not peer.of_type("portfolio.subscribe")
    peer.hold_authorization = False
    cold.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cold
    snapshot = await second
    assert snapshot["portfolio"] == "second" and client.connected
    assert len(peer.sockets) == 2


async def test_old_close_and_reader_cannot_clear_new_generation(local_client, monkeypatch):
    client, peer = local_client
    await client.subscribe_portfolio(robot_id="1", portfolio="old")
    old_ws = client._ws
    original_close = old_ws.close
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_close():
        entered.set()
        await release.wait()
        await original_close()

    monkeypatch.setattr(old_ws, "close", slow_close)
    closing = asyncio.create_task(client.close())
    await asyncio.wait_for(entered.wait(), 1)
    snapshot = await client.subscribe_portfolio(robot_id="1", portfolio="new")
    release.set()
    await closing
    assert client.connected and len(peer.sockets) == 2
    assert set(client._subscriptions) == {snapshot["subscription_id"]}


async def test_missing_heartbeat_ack_closes_only_its_socket(local_client):
    client, peer = local_client
    peer.ack_heartbeat = False
    await client.authenticate()
    await until(lambda: not client.connected, timeout=7)
    assert len([raw for _, _, raw in peer.frames if raw == "7"]) == 1


@pytest.mark.parametrize("grouped", [False, True])
async def test_old_budget_wait_failure_does_not_close_replacement_connection(local_client, grouped):
    client, peer = local_client
    await client.authenticate()
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked_budget(_):
        entered.set()
        await release.wait()

    client._outbound_window.record(monotonic(), 9990)
    client._outbound_sleep = blocked_budget
    operation = client._grouped_exchange(
        [("fixture.old", {})], register_subscriptions=False,
    ) if grouped else client.request("fixture.old", {})
    pending = asyncio.create_task(operation)
    await asyncio.wait_for(entered.wait(), 1)
    await client.close()
    snapshot = await client.subscribe_portfolio(robot_id="1", portfolio="replacement")
    release.set()
    if grouped:
        with pytest.raises(ConnectionError):
            await pending
    else:
        assert (await pending)["type"] == "fixture.old"
    assert client.connected and len(peer.sockets) == 2
    assert snapshot["subscription_id"] in client._subscriptions


async def test_heartbeat_deadline_covers_stalled_data_send_lock(local_client, monkeypatch):
    client, peer = local_client
    await client.authenticate()
    ws = client._ws
    original_send = ws.send
    entered, release = asyncio.Event(), asyncio.Event()

    async def stalled_send(payload):
        if "portfolio.subscribe" in payload:
            entered.set()
            await release.wait()
        await original_send(payload)

    monkeypatch.setattr(ws, "send", stalled_send)
    operation = asyncio.create_task(client.subscribe_portfolio(robot_id="1", portfolio="stalled"))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation
        assert client.connected  # Caller cancellation alone does not terminate the shared connection.
        await until(lambda: not client.connected, timeout=7)
        assert not [raw for _, _, raw in peer.frames if raw == "7"]
    finally:
        release.set()
        await until(lambda: not client._send_lock.locked())
