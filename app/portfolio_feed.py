"""In-memory, non-destructive fan-out over privately owned Viking subscriptions."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from websockets.exceptions import ConnectionClosed

from app.viking_client import VikingAPIError, VikingClient, VikingProtocolError

Target = tuple[str, str]
Owner = tuple[str, str]  # OAuth client_id and credential fingerprint; never returned.


class PortfolioFeedError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _merge(target: dict[str, Any], patch: dict[str, Any]) -> None:
    for key, value in patch.items():
        if isinstance(value, dict):
            if value.get("__action") == "del":
                target.pop(key, None)
            else:
                if not isinstance(target.get(key), dict):
                    target[key] = {}
                _merge(target[key], value)
        else:
            target[key] = copy.deepcopy(value)


def _normalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _normalize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    return None if value == -9007199254740992 else value


@dataclass
class _Hub:
    key: tuple[str, str, str]
    client: VikingClient
    stream_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    epoch: str = field(default_factory=lambda: uuid.uuid4().hex)
    seq: int = 0
    value: dict[str, Any] = field(default_factory=dict)
    source_ts: int = 0
    deleted: bool = False
    subscription_id: str | None = None
    refs: int = 0
    ready: bool = False
    error: Exception | None = None
    task: asyncio.Task[None] | None = None
    journal: deque[tuple[dict[str, Any], int, float]] = field(default_factory=deque)
    journal_bytes: int = 0

    def frame(self, kind: str, value: dict[str, Any], **extra: Any) -> dict[str, Any]:
        seconds, nanos = divmod(self.source_ts, 1_000_000_000)
        timestamp = datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%S")
        return {
            "type": kind, "stream_id": self.stream_id,
            "robot_id": self.key[1], "portfolio": self.key[2],
            "epoch": self.epoch, "seq": self.seq,
            "source_ts_ns": str(self.source_ts),
            "source_ts_iso": f"{timestamp}.{nanos:09d}Z",
            "value": copy.deepcopy(value), "deleted": self.deleted, **extra,
        }


@dataclass
class _Feed:
    owner: Owner
    hubs: list[_Hub]
    expires_at: float
    next_stream: int = 0


class PortfolioFeedManager:
    """One drainer per credential/portfolio, separate bounded leases and cursors.

    No source credentials, upstream eid or global cursor are exposed to callers.
    The legacy subscription tools never receive these private subscription ids.
    """

    def __init__(
        self, *, replay_events: int = 1000, replay_bytes: int = 2_000_000,
        replay_seconds: float = 120, max_feeds_per_owner: int = 32,
        reconnect_delay: float = 0.25,
    ):
        self.replay_events = replay_events
        self.replay_bytes = replay_bytes
        self.replay_seconds = replay_seconds
        self.max_feeds_per_owner = max_feeds_per_owner
        self.reconnect_delay = reconnect_delay
        self._hubs: dict[tuple[str, str, str], _Hub] = {}
        self._feeds: dict[str, _Feed] = {}
        self._condition = asyncio.Condition()
        self._lifecycle_lock = asyncio.Lock()
        self._cleanup_task: asyncio.Task[None] | None = None
        self._disposals: set[asyncio.Task[None]] = set()

    def start(self) -> None:
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def open(
        self, owner: Owner, client: VikingClient, targets: list[Target], *,
        expires_at: float, feed_id: str | None = None,
    ) -> dict[str, Any]:
        if not 1 <= len(targets) <= 100 or len(set(targets)) != len(targets):
            raise PortfolioFeedError("scope_mismatch", "Supply 1..100 unique portfolios")
        if any(not robot or not portfolio for robot, portfolio in targets):
            raise ValueError("Portfolio identities must not be empty")
        if expires_at <= time.time() or expires_at > time.time() + 301:
            raise ValueError("Feed lease must expire within 300 seconds")
        if feed_id is not None:
            feed = self._require(feed_id, owner)
            if {(h.key[1], h.key[2]) for h in feed.hubs} != set(targets):
                raise PortfolioFeedError("scope_mismatch", "Renewal cannot change the portfolio set")
            for hub in feed.hubs:
                if hub.error:
                    raise hub.error
                if not hub.ready:
                    raise ConnectionError("Portfolio feed is reconnecting; retry renewal")
            feed.expires_at = expires_at
            return self._opened(feed_id, feed, "renew")
        async with self._lifecycle_lock:
            if sum(feed.owner == owner for feed in self._feeds.values()) >= self.max_feeds_per_owner:
                raise PortfolioFeedError("capacity_exceeded", "Too many active feeds; close unused feeds")
            hubs = []
            created = []
            for robot, portfolio in targets:
                key = (owner[1], robot, portfolio)
                hub = self._hubs.get(key)
                if hub is None or hub.deleted or hub.error is not None:
                    hub = _Hub(key, client)
                    self._hubs[key] = hub
                    created.append(hub)
                hubs.append(hub)
                hub.refs += 1  # Reserve during setup, so another lease can safely close/expire.
            # Bound initial fan-out while retaining the one-WS-per-credentials pool.
            semaphore = asyncio.Semaphore(8)

            async def initialize(hub: _Hub) -> None:
                async with semaphore:
                    await self._subscribe(hub)

            tasks = [asyncio.create_task(initialize(hub)) for hub in created]
            try:
                if tasks:
                    await asyncio.wait_for(asyncio.gather(*tasks), max(0, expires_at - time.time()))
                if expires_at <= time.time():
                    raise ValueError("Feed lease expired during subscription setup")
                for hub in hubs:
                    if hub.error:
                        raise hub.error
                    if not hub.ready:
                        raise ConnectionError("Portfolio feed is reconnecting; retry open")
                feed_id = uuid.uuid4().hex
                feed = _Feed(owner, hubs, expires_at)
                self._feeds[feed_id] = feed
                for hub in hubs:
                    if hub.task is None and not hub.deleted:
                        hub.task = asyncio.create_task(self._consume(hub))
                return self._opened(feed_id, feed, "initial")
            except BaseException:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                unused = []
                for hub in hubs:
                    hub.refs -= 1
                    if not hub.refs:
                        if self._hubs.get(hub.key) is hub:
                            self._hubs.pop(hub.key, None)
                        unused.append(hub)
                await asyncio.gather(*(self._dispose(hub) for hub in unused))
                raise

    def _opened(self, feed_id: str, feed: _Feed, reason: str) -> dict[str, Any]:
        return self._response(feed_id, feed, [
            hub.frame("snapshot", hub.value, reset_reason=reason) for hub in feed.hubs
        ])

    def _require(self, feed_id: str, owner: Owner) -> _Feed:
        feed = self._feeds.get(feed_id)
        if feed is None or feed.owner != owner:
            raise PortfolioFeedError("feed_not_found", "Unknown portfolio feed")
        if feed.expires_at <= time.time():
            raise PortfolioFeedError("feed_expired", "Portfolio feed lease expired; open a new feed")
        return feed

    async def read(
        self, owner: Owner, feed_id: str, cursors: list[dict[str, Any]], *,
        wait_seconds: float = 25, max_events: int = 100,
    ) -> dict[str, Any]:
        if not 0 <= wait_seconds <= 30 or not 1 <= max_events <= 500:
            raise ValueError("wait_seconds must be 0..30 and max_events 1..500")
        deadline = time.monotonic() + wait_seconds
        async with self._condition:
            while True:
                feed = self._require(feed_id, owner)
                cursor_map = {cursor["stream_id"]: cursor for cursor in cursors}
                if len(cursor_map) != len(cursors) or set(cursor_map) != {h.stream_id for h in feed.hubs}:
                    raise PortfolioFeedError(
                        "cursor_invalid", "Cursors must cover exactly the feed's streams",
                    )
                items = self._collect(feed, cursor_map)
                remaining = min(deadline - time.monotonic(), feed.expires_at - time.time())
                if items or remaining <= 0:
                    feed.next_stream = (feed.next_stream + 1) % len(feed.hubs)
                    return self._response(feed_id, feed, items[:max_events], len(items) > max_events)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._condition.wait(), timeout=remaining)

    def _collect(self, feed: _Feed, cursors: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        streams: list[deque[dict[str, Any]]] = []
        for hub in feed.hubs[feed.next_stream:] + feed.hubs[:feed.next_stream]:
            if hub.error:
                raise hub.error
            if not hub.ready:
                continue
            self._trim(hub)
            cursor = cursors[hub.stream_id]
            seq = cursor["seq"]
            if type(seq) is not int or seq < 0:
                raise PortfolioFeedError("cursor_invalid", "Cursor seq must be a non-negative integer")
            earliest = hub.journal[0][0]["seq"] if hub.journal else hub.seq + 1
            if cursor["epoch"] != hub.epoch:
                frames = [hub.frame("snapshot", hub.value, reset_reason="epoch_changed")]
            elif seq > hub.seq or seq < earliest - 1:
                frames = [hub.frame("snapshot", hub.value, reset_reason="cursor_gap")]
            else:
                frames = [record for record, _, _ in hub.journal if record["seq"] > seq]
            streams.append(deque(frames))
        # Round-robin within a batch avoids one hot stream consuming the whole default batch.
        items = []
        while any(streams):
            for stream in streams:
                if stream:
                    items.append(stream.popleft())
        return items

    @staticmethod
    def _response(
        feed_id: str, feed: _Feed, items: list[dict[str, Any]], more: bool = False,
    ) -> dict[str, Any]:
        return {
            "protocol_version": 1, "feed_id": feed_id,
            "lease_expires_at": feed.expires_at,
            "items": copy.deepcopy(items), "more_available": more,
            "data_status": "ok" if items else "empty",
            "notes": [] if items else ["No events after the supplied cursors within the wait window."],
            "streams": [{"stream_id": h.stream_id, "state": (
                "deleted" if h.deleted else "live" if h.ready else "reconnecting"
            )} for h in feed.hubs],
        }

    async def _subscribe(self, hub: _Hub) -> None:
        event = await hub.client.subscribe_portfolio(robot_id=hub.key[1], portfolio=hub.key[2])
        hub.subscription_id = event["subscription_id"]
        async with self._condition:
            hub.epoch = uuid.uuid4().hex
            hub.seq = 0
            hub.source_ts = event["ts"]
            hub.value = _normalize(event["value"])
            hub.deleted = event["deleted"]
            hub.ready = True
            hub.error = None
            hub.journal.clear()
            hub.journal_bytes = 0
            self._condition.notify_all()

    async def _consume(self, hub: _Hub) -> None:
        delay = self.reconnect_delay
        while True:
            try:
                if hub.subscription_id is None:
                    await self._subscribe(hub)
                result = await hub.client.get_portfolio_updates(
                    hub.subscription_id, wait_seconds=1, max_events=500,
                )
                async with self._condition:
                    for event in result["events"]:
                        self._append(hub, event)
                    self._condition.notify_all()
                if hub.deleted:
                    hub.subscription_id = None  # Viking auto-unsubscribes a deleted portfolio.
                    return
                if not result["active"]:
                    raise ConnectionError("Portfolio upstream disconnected")
                delay = self.reconnect_delay
            except VikingAPIError as exc:
                async with self._condition:
                    hub.error = exc
                    hub.ready = False
                    self._condition.notify_all()
                return
            except (OSError, ConnectionClosed, ValueError, VikingProtocolError):
                async with self._condition:
                    hub.ready = False
                    self._condition.notify_all()
                await self._unsubscribe(hub)
                await asyncio.sleep(delay)
                delay = min(max(delay * 2, 0.01), 10)
            except Exception as exc:
                async with self._condition:
                    hub.error = exc
                    hub.ready = False
                    self._condition.notify_all()
                return

    def _append(self, hub: _Hub, event: dict[str, Any]) -> None:
        hub.seq += 1
        hub.source_ts = event["ts"]
        hub.deleted = event["deleted"]
        value = _normalize(event["value"])
        if event["r"] == "s" or hub.deleted:
            hub.value = copy.deepcopy(value)
        else:
            _merge(hub.value, value)
        frame = hub.frame("snapshot" if event["r"] == "s" else "patch", value)
        if event["r"] == "s":
            frame["reset_reason"] = "upstream_snapshot"
        size = len(json.dumps(frame, ensure_ascii=False, separators=(",", ":")).encode())
        hub.journal.append((frame, size, time.monotonic()))
        hub.journal_bytes += size
        self._trim(hub)

    def _trim(self, hub: _Hub) -> None:
        cutoff = time.monotonic() - self.replay_seconds
        while hub.journal and (
            len(hub.journal) > self.replay_events or hub.journal_bytes > self.replay_bytes
            or hub.journal[0][2] < cutoff
        ):
            _, size, _ = hub.journal.popleft()
            hub.journal_bytes -= size

    async def close_feed(self, owner: Owner, feed_id: str) -> dict[str, Any]:
        feed = self._feeds.get(feed_id)
        if feed is None or feed.owner != owner:
            raise PortfolioFeedError("feed_not_found", "Unknown portfolio feed")
        await self._release(feed_id)
        return {"feed_id": feed_id, "closed": True}

    async def _release(self, feed_id: str) -> None:
        feed = self._feeds.pop(feed_id, None)
        if feed is None:
            return
        unused = []
        for hub in feed.hubs:
            hub.refs -= 1
            if not hub.refs:
                if self._hubs.get(hub.key) is hub:
                    self._hubs.pop(hub.key, None)
                unused.append(hub)
        async with self._condition:
            self._condition.notify_all()
        tasks = [asyncio.create_task(self._dispose(hub)) for hub in unused]
        for task in tasks:
            self._disposals.add(task)
            task.add_done_callback(self._disposals.discard)
        if tasks:
            await asyncio.shield(asyncio.gather(*tasks))

    async def _unsubscribe(self, hub: _Hub) -> None:
        subscription_id = hub.subscription_id
        if subscription_id is not None:
            with contextlib.suppress(
                ValueError, OSError, ConnectionClosed, VikingProtocolError, VikingAPIError,
            ):
                await hub.client.unsubscribe_portfolio(subscription_id)
            hub.subscription_id = None

    async def _dispose(self, hub: _Hub) -> None:
        if hub.task:
            hub.task.cancel()
            await asyncio.gather(hub.task, return_exceptions=True)
        await self._unsubscribe(hub)

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(1)
            expired = [fid for fid, feed in self._feeds.items() if feed.expires_at <= time.time()]
            await asyncio.gather(*(self._release(feed_id) for feed_id in expired))

    async def close(self) -> None:
        if self._cleanup_task:
            self._cleanup_task.cancel()
            await asyncio.gather(self._cleanup_task, return_exceptions=True)
            self._cleanup_task = None
        async with self._lifecycle_lock:
            for feed_id in list(self._feeds):
                await self._release(feed_id)
        if self._disposals:
            await asyncio.gather(*self._disposals, return_exceptions=True)
