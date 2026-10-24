import asyncio
import time
from types import SimpleNamespace

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session

from app import feed_tools
from app.credentials import VikingCredentials
from app.oauth import OAUTH_SCOPE
from app.portfolio_feed import PortfolioFeedManager
from app.viking_client import VikingAPIError, VikingProtocolError

OWNER = ("client", "credential-fingerprint")


class FakeClient:
    def __init__(self):
        self.subscriptions = {}
        self.queues = {}
        self.opens = []
        self.closes = []
        self.initial = {"name": "p", "uf1": {"v": 1, "c": "old"}, "securities": {}}
        self.fail_portfolio = None
        self.read_entered = asyncio.Event()

    async def subscribe_portfolio(self, *, robot_id, portfolio):
        if portfolio == self.fail_portfolio:
            raise VikingAPIError("Denied", code=403)
        self.opens.append((robot_id, portfolio))
        sid = f"private-{len(self.opens)}"
        self.subscriptions[sid] = portfolio
        self.queues[sid] = asyncio.Queue()
        return {
            "subscription_id": sid, "ts": 1780000000000000001, "r": "s",
            "value": {**self.initial, "name": portfolio}, "deleted": False,
        }

    async def get_portfolio_updates(self, sid, *, wait_seconds, max_events):
        self.read_entered.set()
        try:
            value = await asyncio.wait_for(self.queues[sid].get(), wait_seconds)
        except TimeoutError:
            return {"events": [], "active": True}
        if isinstance(value, Exception):
            raise value
        if value["deleted"]:
            self.subscriptions.pop(sid, None)
        return {"events": [value], "active": not value["deleted"]}

    async def unsubscribe_portfolio(self, sid):
        self.closes.append(sid)
        self.subscriptions.pop(sid, None)

    def emit(self, portfolio, value, *, result="u", deleted=False):
        sid = next(s for s, p in self.subscriptions.items() if p == portfolio)
        self.queues[sid].put_nowait({
            "ts": 1780000000000000101, "r": result, "value": value, "deleted": deleted,
        })


@pytest.fixture
async def manager():
    value = PortfolioFeedManager(reconnect_delay=0)
    yield value
    await value.close()


def cursors(result):
    return [{key: item[key] for key in ("stream_id", "epoch", "seq")} for item in result["items"]]


async def opened(manager, client, targets=None, **kwargs):
    return await manager.open(
        OWNER, client, targets or [("1", "p")], expires_at=time.time() + 300, **kwargs,
    )


async def wait_until(manager, predicate):
    async with manager._condition:
        await asyncio.wait_for(manager._condition.wait_for(predicate), 2)


async def test_shared_consumer_independent_replay_and_refcount(manager):
    client = FakeClient()
    first = await opened(manager, client)
    second = await opened(manager, client)
    assert first["feed_id"] != second["feed_id"]
    assert len(client.opens) == 1
    assert first["items"][0]["stream_id"] == second["items"][0]["stream_id"]
    client.emit("p", {"name": "p", "pos": 7})
    one = await manager.read(OWNER, first["feed_id"], cursors(first), wait_seconds=2)
    two = await manager.read(OWNER, second["feed_id"], cursors(second), wait_seconds=0)
    repeat = await manager.read(OWNER, first["feed_id"], cursors(first), wait_seconds=0)
    assert one["items"] == two["items"] == repeat["items"]
    assert one["items"][0]["seq"] == 1
    assert "subscription_id" not in one and "eid" not in str(one)
    await manager.close_feed(OWNER, first["feed_id"])
    assert client.closes == []
    await manager.close_feed(OWNER, second["feed_id"])
    assert len(client.closes) == 1


async def test_multiplex_wakes_on_any_without_waiting_for_quiet_first_portfolio(manager):
    client = FakeClient()
    feed = await opened(manager, client, [("1", "quiet"), ("1", "busy")])
    pending = asyncio.create_task(manager.read(OWNER, feed["feed_id"], cursors(feed), wait_seconds=30))
    client.emit("busy", {"name": "busy", "unknown": {"future": [1, 2]}})
    result = await asyncio.wait_for(pending, 1)
    assert [item["portfolio"] for item in result["items"]] == ["busy"]
    assert result["items"][0]["value"]["unknown"] == {"future": [1, 2]}


async def test_gap_snapshot_merges_nested_deletions_and_preserves_source_timestamp(manager):
    manager.replay_events = 1
    client = FakeClient()
    client.initial["securities"] = {"x": {"sec_key": "x", "price": 1}, "y": {"sec_key": "y"}}
    feed = await opened(manager, client)
    client.emit("p", {"name": "p", "uf1": {"v": 2}, "securities": {"x": {"__action": "del"}}})
    client.emit("p", {"name": "p", "new_field": -9007199254740992})
    hub = next(iter(manager._hubs.values()))
    await wait_until(manager, lambda: hub.seq == 2)
    result = await manager.read(OWNER, feed["feed_id"], cursors(feed), wait_seconds=0)
    snapshot = result["items"][0]
    assert snapshot["type"] == "snapshot" and snapshot["reset_reason"] == "cursor_gap"
    assert snapshot["value"]["uf1"] == {"v": 2, "c": "old"}
    assert snapshot["value"]["securities"] == {"y": {"sec_key": "y"}}
    assert snapshot["value"]["new_field"] is None
    assert snapshot["source_ts_ns"] == "1780000000000000101"
    assert snapshot["source_ts_iso"].endswith(".000000101Z")
    assert snapshot["seq"] == 2


@pytest.mark.parametrize("bound", ["bytes", "age"])
async def test_replay_memory_and_age_bounds_reset_instead_of_losing_updates(manager, bound):
    if bound == "bytes":
        manager.replay_bytes = 1
    else:
        manager.replay_seconds = -1
    client = FakeClient()
    feed = await opened(manager, client)
    client.emit("p", {"name": "p", "pos": 3})
    hub = next(iter(manager._hubs.values()))
    await wait_until(manager, lambda: hub.seq == 1)
    result = await manager.read(OWNER, feed["feed_id"], cursors(feed), wait_seconds=0)
    assert result["items"][0]["reset_reason"] == "cursor_gap"
    assert result["items"][0]["value"]["pos"] == 3
    assert not hub.journal


async def test_repeated_snapshot_replaces_state_and_deletion_is_terminal(manager):
    client = FakeClient()
    feed = await opened(manager, client)
    client.emit("p", {"name": "p", "securities": {}, "new": True}, result="s")
    result = await manager.read(OWNER, feed["feed_id"], cursors(feed), wait_seconds=2)
    assert result["items"][0]["type"] == "snapshot"
    assert "uf1" not in result["items"][0]["value"]
    client.emit("p", {"name": "p", "__action": "del"}, deleted=True)
    deleted = await manager.read(OWNER, feed["feed_id"], cursors(result), wait_seconds=2)
    assert deleted["items"][0]["deleted"] is True
    assert deleted["streams"][0]["state"] == "deleted"
    assert len(client.opens) == 1


@pytest.mark.parametrize("failure", [ConnectionError("disconnected"), VikingProtocolError("overflow")])
async def test_disconnect_and_overflow_change_epoch_and_reset(manager, failure):
    client = FakeClient()
    feed = await opened(manager, client)
    client.queues["private-1"].put_nowait(failure)
    result = await manager.read(OWNER, feed["feed_id"], cursors(feed), wait_seconds=2)
    assert result["items"][0]["type"] == "snapshot"
    assert result["items"][0]["epoch"] != feed["items"][0]["epoch"]
    assert result["items"][0]["reset_reason"] == "epoch_changed"
    assert len(client.opens) == 2


async def test_api_error_keeps_code_and_is_not_retried(manager):
    client = FakeClient()
    feed = await opened(manager, client)
    error = VikingAPIError("Permission removed", code=403, response={"data": {"code": 403}})
    client.queues["private-1"].put_nowait(error)
    with pytest.raises(VikingAPIError) as caught:
        await manager.read(OWNER, feed["feed_id"], cursors(feed), wait_seconds=2)
    assert caught.value is error
    assert len(client.opens) == 1


async def test_explicit_fresh_open_can_replace_terminal_hub_without_old_close_removing_it(manager):
    client = FakeClient()
    old = await opened(manager, client)
    client.emit("p", {"name": "p", "__action": "del"}, deleted=True)
    await manager.read(OWNER, old["feed_id"], cursors(old), wait_seconds=2)
    fresh = await opened(manager, client)
    assert fresh["items"][0]["stream_id"] != old["items"][0]["stream_id"]
    assert fresh["items"][0]["deleted"] is False
    await manager.close_feed(OWNER, old["feed_id"])
    third = await opened(manager, client)
    assert third["items"][0]["stream_id"] == fresh["items"][0]["stream_id"]
    assert len(client.opens) == 2


async def test_scope_cursors_and_immutable_renewal(manager):
    client = FakeClient()
    feed = await opened(manager, client)
    with pytest.raises(ValueError, match="Unknown"):
        await manager.read(("other-client", OWNER[1]), feed["feed_id"], cursors(feed), wait_seconds=0)
    with pytest.raises(ValueError, match="exactly"):
        await manager.read(OWNER, feed["feed_id"], [], wait_seconds=0)
    with pytest.raises(ValueError, match="exactly"):
        await manager.read(OWNER, feed["feed_id"], cursors(feed) * 2, wait_seconds=0)
    with pytest.raises(ValueError, match="cannot change"):
        await opened(manager, client, [("1", "other")], feed_id=feed["feed_id"])
    renewed = await opened(manager, client, feed_id=feed["feed_id"])
    assert renewed["feed_id"] == feed["feed_id"]
    assert renewed["items"][0]["reset_reason"] == "renew"
    assert len(client.opens) == 1


async def test_lease_expiry_wakes_wait_and_releases_upstream(manager):
    client = FakeClient()
    feed = await manager.open(OWNER, client, [("1", "p")], expires_at=time.time() + 0.05)
    with pytest.raises(ValueError, match="expired"):
        await manager.read(OWNER, feed["feed_id"], cursors(feed), wait_seconds=2)
    manager.start()
    for _ in range(150):
        if client.closes:
            break
        await asyncio.sleep(0.01)
    assert client.closes == ["private-1"]


async def test_close_wakes_read_and_failed_open_does_not_leak(manager):
    client = FakeClient()
    feed = await opened(manager, client)
    pending = asyncio.create_task(manager.read(OWNER, feed["feed_id"], cursors(feed), wait_seconds=30))
    await manager.close_feed(OWNER, feed["feed_id"])
    with pytest.raises(ValueError, match="Unknown"):
        await asyncio.wait_for(pending, 1)
    client.fail_portfolio = "denied"
    with pytest.raises(VikingAPIError):
        await opened(manager, client, [("1", "good"), ("1", "denied")])
    assert not manager._hubs and not manager._feeds and not client.subscriptions


async def test_small_batches_replay_without_starving_second_stream(manager):
    client = FakeClient()
    feed = await opened(manager, client, [("1", "a"), ("1", "b")])
    for name in ("a", "b"):
        client.emit(name, {"name": name, "pos": 1})
    await wait_until(manager, lambda: all(h.seq == 1 for h in manager._hubs.values()))
    first = await manager.read(OWNER, feed["feed_id"], cursors(feed), wait_seconds=0, max_events=1)
    second = await manager.read(OWNER, feed["feed_id"], cursors(feed), wait_seconds=0, max_events=1)
    assert first["more_available"] and second["more_available"]
    assert first["items"][0]["stream_id"] != second["items"][0]["stream_id"]


@pytest.fixture
async def tool_context(manager, monkeypatch):
    client = FakeClient()
    access = SimpleNamespace(
        token="synthetic-test-token", client_id="client", scopes=[OAUTH_SCOPE],
        expires_at=int(time.time()) + 200,
    )
    state = SimpleNamespace(access=access, valid=True)

    async def load_access_token(token):
        assert token == "synthetic-test-token"
        return state.access if state.valid and state.access.expires_at > time.time() else None

    provider = SimpleNamespace(
        load_access_token=load_access_token,
        credentials_for_access_token=lambda token: VikingCredentials("fixture", "fixture", "fixture"),
    )
    monkeypatch.setattr(feed_tools, "get_access_token", lambda: access)
    mcp = FastMCP("Feed tests")
    feed_tools.register_feed_tools(
        mcp, manager=manager, provider=provider, clients=SimpleNamespace(get=lambda credentials: client),
    )
    class Session:
        async def list_tools(self):
            async with create_connected_server_and_client_session(mcp, raise_exceptions=True) as session:
                return await session.list_tools()

        async def call_tool(self, name, arguments):
            async with create_connected_server_and_client_session(mcp, raise_exceptions=True) as session:
                return await session.call_tool(name, arguments)

    return Session(), state, client


async def test_tool_schema_and_full_mcp_flow(tool_context):
    session, state, client = tool_context
    listing = await session.list_tools()
    schemas = {tool.name: tool for tool in listing.tools}
    assert set(schemas) == {"open_portfolio_feed", "read_portfolio_feed", "close_portfolio_feed"}
    assert all(t.annotations.readOnlyHint and not t.annotations.destructiveHint for t in listing.tools)
    assert schemas["read_portfolio_feed"].inputSchema["properties"]["wait_seconds"]["maximum"] == 30
    result = await session.call_tool("open_portfolio_feed", {
        "portfolios": [{"robot_id": 1, "portfolio": "p"}],
    })
    assert not result.isError
    feed = result.structuredContent
    assert feed["lease_expires_at"] == state.access.expires_at
    assert feed["items"][0]["robot_id"] == "1"
    client.emit("p", {"name": "p", "pos": 2})
    read = await session.call_tool("read_portfolio_feed", {
        "feed_id": feed["feed_id"], "cursors": cursors(feed), "wait_seconds": 2,
    })
    assert not read.isError and read.structuredContent["items"][0]["value"]["pos"] == 2
    close = await session.call_tool("close_portfolio_feed", {"feed_id": feed["feed_id"]})
    assert close.structuredContent["closed"] is True


@pytest.mark.parametrize("invalid", ["missing_scope", "revoked", "expired"])
async def test_every_feed_tool_rechecks_authorization(tool_context, invalid):
    session, state, client = tool_context
    result = await session.call_tool(
        "open_portfolio_feed", {"portfolios": [{"robot_id": "1", "portfolio": "p"}]},
    )
    feed = result.structuredContent
    if invalid == "missing_scope":
        state.access.scopes = []
    elif invalid == "revoked":
        state.valid = False
    else:
        state.access.expires_at = 0
    arguments = {
        "open_portfolio_feed": {"portfolios": [{"robot_id": "1", "portfolio": "p"}]},
        "read_portfolio_feed": {"feed_id": feed["feed_id"], "cursors": cursors(feed), "wait_seconds": 0},
        "close_portfolio_feed": {"feed_id": feed["feed_id"]},
    }
    for name, args in arguments.items():
        denied = await session.call_tool(name, args)
        assert denied.isError and denied.structuredContent["error_type"] == "PermissionError"
    assert len(client.opens) == 1


async def test_revocation_during_long_poll_does_not_release_data(tool_context, manager, monkeypatch):
    session, state, client = tool_context
    result = await session.call_tool(
        "open_portfolio_feed", {"portfolios": [{"robot_id": "1", "portfolio": "p"}]},
    )
    feed = result.structuredContent
    entered = asyncio.Event()
    original_read = manager.read

    async def observe_read(*args, **kwargs):
        entered.set()
        return await original_read(*args, **kwargs)

    monkeypatch.setattr(manager, "read", observe_read)
    pending = asyncio.create_task(session.call_tool("read_portfolio_feed", {
        "feed_id": feed["feed_id"], "cursors": cursors(feed), "wait_seconds": 2,
    }))
    await asyncio.wait_for(entered.wait(), 1)
    state.valid = False
    client.emit("p", {"name": "p", "pos": 999})
    denied = await pending
    assert denied.isError and "items" not in denied.structuredContent


@pytest.mark.parametrize("portfolios", [
    [{"robot_id": True, "portfolio": "p"}],
    [{"robot_id": 9007199254740992, "portfolio": "p"}],
    [{"robot_id": "-1", "portfolio": "p"}],
    [{"robot_id": "1", "portfolio": ""}],
    [{"robot_id": "1", "portfolio": "p", "credentials": "not-accepted"}],
    [{"robot_id": "1", "portfolio": "p"}] * 2,
])
async def test_invalid_scope_is_rejected_before_subscribing(tool_context, portfolios):
    session, _, client = tool_context
    result = await session.call_tool("open_portfolio_feed", {"portfolios": portfolios})
    assert result.isError and not client.opens


async def test_decimal_robot_id_preserves_twenty_digits_and_leading_zeros(tool_context):
    session, _, client = tool_context
    identity = "00123456789012345678"
    result = await session.call_tool("open_portfolio_feed", {
        "portfolios": [{"robot_id": identity, "portfolio": "p"}],
    })
    assert result.structuredContent["items"][0]["robot_id"] == identity
    assert client.opens == [(identity, "p")]


async def test_pending_open_does_not_block_renew_or_release_reserved_hub(manager):
    client = FakeClient()
    feed = await opened(manager, client)
    subscribe = client.subscribe_portfolio
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_subscribe(*, robot_id, portfolio):
        if portfolio == "slow":
            entered.set()
            await release.wait()
        return await subscribe(robot_id=robot_id, portfolio=portfolio)

    client.subscribe_portfolio = slow_subscribe
    pending = asyncio.create_task(opened(manager, client, [("1", "p"), ("1", "slow")]))
    await asyncio.wait_for(entered.wait(), 1)
    await asyncio.wait_for(opened(manager, client, feed_id=feed["feed_id"]), 1)
    await asyncio.wait_for(manager.close_feed(OWNER, feed["feed_id"]), 1)
    assert client.closes == []  # Pending open still holds a reservation on p.
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert not manager._hubs and not client.subscriptions
    assert client.closes == ["private-1"]


async def test_mcp_machine_readable_expiry_and_unknown_feed_errors(tool_context, manager):
    session, _, _ = tool_context
    result = await session.call_tool("open_portfolio_feed", {
        "portfolios": [{"robot_id": "1", "portfolio": "p"}],
    })
    feed = result.structuredContent
    manager._feeds[feed["feed_id"]].expires_at = time.time() - 1
    expired = await session.call_tool("read_portfolio_feed", {
        "feed_id": feed["feed_id"], "cursors": cursors(feed), "wait_seconds": 0,
    })
    assert expired.structuredContent["feed_error"] == "feed_expired"
    unknown = await session.call_tool("close_portfolio_feed", {"feed_id": "does-not-exist"})
    assert unknown.structuredContent["feed_error"] == "feed_not_found"
