from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
import zlib
from collections.abc import Iterable
from dataclasses import dataclass
from time import monotonic
from typing import Any

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed
from websockets.protocol import State

from app.config import Settings
from app.credentials import VikingCredentials

logger = logging.getLogger(__name__)


class VikingAPIError(RuntimeError):
    """Error returned by the Viking WebSocket API."""

    def __init__(
        self,
        message: str,
        code: int | None = None,
        *,
        response: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.response = response


class VikingProtocolError(RuntimeError):
    """Malformed or unexpected response returned by the Viking API."""


@dataclass
class _Subscription:
    message_type: str
    queue: asyncio.Queue[dict[str, Any]]
    overflowed: bool = False
    request_data: dict[str, Any] | None = None


@dataclass
class _PooledClient:
    client: VikingClient
    last_used: float


class VikingClientPool:
    """Reuse one persistent Viking WebSocket per set of user credentials."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._clients: dict[str, _PooledClient] = {}
        self._cleanup_task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    def get(self, credentials: VikingCredentials) -> VikingClient:
        fingerprint = credentials.fingerprint
        pooled = self._clients.get(fingerprint)
        if pooled is None:
            pooled = _PooledClient(
                client=VikingClient(self.settings, credentials),
                last_used=monotonic(),
            )
            self._clients[fingerprint] = pooled
        else:
            pooled.last_used = monotonic()
        return pooled.client

    async def _cleanup_loop(self) -> None:
        interval = min(60, max(10, self.settings.credentials_idle_ttl_seconds // 4))
        try:
            while True:
                await asyncio.sleep(interval)
                cutoff = monotonic() - self.settings.credentials_idle_ttl_seconds
                expired = [
                    (fingerprint, pooled)
                    for fingerprint, pooled in self._clients.items()
                    if pooled.last_used <= cutoff
                ]
                for fingerprint, pooled in expired:
                    self._clients.pop(fingerprint, None)
                    await pooled.client.close()
                if expired:
                    logger.info("Removed %s idle Viking credential session(s) from RAM", len(expired))
        except asyncio.CancelledError:
            raise

    async def close(self) -> None:
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            await asyncio.gather(self._cleanup_task, return_exceptions=True)
            self._cleanup_task = None
        clients = [pooled.client for pooled in self._clients.values()]
        self._clients.clear()
        if clients:
            await asyncio.gather(*(client.close() for client in clients), return_exceptions=True)


class VikingClient:
    """Concurrent request/response client over one persistent Viking WebSocket."""

    def __init__(self, settings: Settings, credentials: VikingCredentials):
        self.settings = settings
        self.credentials = credentials
        self._ws: ClientConnection | None = None
        self._connect_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._subscriptions: dict[str, _Subscription] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None

    @property
    def connected(self) -> bool:
        return self._ws is not None and self._ws.state is State.OPEN

    async def authenticate(self) -> None:
        """Validate credentials by completing Viking WebSocket authorization."""
        await self._ensure_connected()

    async def close(self) -> None:
        ws, self._ws = self._ws, None
        current = asyncio.current_task()
        for task in (self._heartbeat_task, self._reader_task):
            if task and task is not current:
                task.cancel()
        self._heartbeat_task = None
        self._reader_task = None
        if ws is not None:
            await ws.close()
        self._fail_pending(ConnectionError("Viking WebSocket connection closed"))

    async def request(
        self,
        message_type: str,
        data: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Send an idempotent API request and wait for its matching eid response."""
        timeout = timeout or self.settings.viking_request_timeout_seconds
        last_error: BaseException | None = None

        for attempt in range(2):
            try:
                await self._ensure_connected()
                return await self._request_connected(message_type, data or {}, timeout=timeout)
            except (TimeoutError, ConnectionClosed, ConnectionError) as exc:
                last_error = exc
                logger.warning(
                    "Viking request %s failed on attempt %s: %s",
                    message_type,
                    attempt + 1,
                    type(exc).__name__,
                )
                await self.close()

        assert last_error is not None
        raise ConnectionError(f"Viking API request failed after reconnect: {message_type}") from last_error

    async def list_portfolios(self) -> list[dict[str, Any]]:
        """Return all accessible portfolios and whether history is enabled."""
        snapshot = await self.subscribe_available_portfolios()
        all_rows = snapshot["portfolios_add"]
        try:
            await self.unsubscribe_available_portfolios(snapshot["subscription_id"])
        except Exception:
            logger.warning("Could not unsubscribe from available portfolio list", exc_info=True)

        history_response = await self.request("available_portfolio_list.get_with_history", {})
        history_rows = history_response.get("data", {}).get("portfolios", [])
        history_ids = {(str(row[0]), str(row[1])) for row in history_rows if len(row) >= 2}

        result = []
        for row in all_rows:
            robot_id = row["robot_id"]
            portfolio = row["portfolio"]
            result.append(
                {
                    "robot_id": robot_id,
                    "portfolio": portfolio,
                    "owner": row["owner"],
                    "history_available": (robot_id, portfolio) in history_ids,
                }
            )

        result.sort(key=lambda item: (item["robot_id"], item["portfolio"]))
        return result

    async def subscribe_available_portfolios(self) -> dict[str, Any]:
        """Subscribe and return the initial complete portfolio-list snapshot."""
        response = await self._subscribe("available_portfolio_list.subscribe", {})
        subscription_id = self._required_str(response, "eid")
        try:
            event = self._parse_portfolio_event(
                response,
                subscription_id=subscription_id,
                allowed_results={"s"},
                require_portfolios_add=True,
            )
            if event["portfolios_del"]:
                raise VikingProtocolError(
                    "Initial portfolio snapshot unexpectedly contains portfolios_del"
                )
            return {"subscription_id": subscription_id, **event}
        except BaseException:
            self._subscriptions.pop(subscription_id, None)
            await self.close()
            raise

    async def get_available_portfolio_updates(
        self,
        subscription_id: str,
        *,
        wait_seconds: float = 0,
        max_events: int = 100,
    ) -> dict[str, Any]:
        """Return buffered add/delete events for an active portfolio-list subscription."""
        subscription = self._subscriptions.get(subscription_id)
        if subscription is None or subscription.message_type != "available_portfolio_list.subscribe":
            raise ValueError("Unknown or inactive available-portfolio subscription_id")
        if subscription.overflowed:
            raise VikingProtocolError(
                "Available-portfolio subscription buffer overflowed and events were lost; "
                "unsubscribe and create a new subscription"
            )
        if not 0 <= wait_seconds <= 30:
            raise ValueError("wait_seconds must be in range 0..30")
        if not 1 <= max_events <= 500:
            raise ValueError("max_events must be in range 1..500")

        messages: list[dict[str, Any]] = []
        if wait_seconds and subscription.queue.empty():
            with contextlib.suppress(TimeoutError):
                messages.append(
                    await asyncio.wait_for(subscription.queue.get(), timeout=wait_seconds)
                )
        while len(messages) < max_events:
            try:
                messages.append(subscription.queue.get_nowait())
            except asyncio.QueueEmpty:
                break

        events = []
        for message in messages:
            self._validate_response_identity(
                message,
                expected_type="available_portfolio_list.subscribe",
                expected_eid=subscription_id,
            )
            if message.get("r") == "e":
                self._raise_api_error(message)
            events.append(
                self._parse_portfolio_event(
                    message,
                    subscription_id=subscription_id,
                    allowed_results={"s", "u"},
                )
            )
        return {
            "subscription_id": subscription_id,
            "event_count": len(events),
            "events": events,
            "more_available": not subscription.queue.empty(),
        }

    async def unsubscribe_available_portfolios(self, subscription_id: str) -> dict[str, Any]:
        """Unsubscribe from portfolio-list updates and return the full acknowledgement."""
        subscription = self._subscriptions.get(subscription_id)
        if subscription is None or subscription.message_type != "available_portfolio_list.subscribe":
            raise ValueError("Unknown or inactive available-portfolio subscription_id")
        response = await self.request(
            "available_portfolio_list.unsubscribe",
            {"sub_eid": subscription_id},
        )
        self._validate_response_identity(
            response,
            expected_type="available_portfolio_list.unsubscribe",
        )
        result = self._required_str(response, "r")
        if result != "p":
            raise VikingProtocolError(
                "available_portfolio_list.unsubscribe returned an unexpected result; expected r='p'"
            )
        parsed = {
            "subscription_id": subscription_id,
            "type": self._required_str(response, "type"),
            "eid": self._required_str(response, "eid"),
            "ts": self._required_int(response, "ts"),
            "r": result,
            "result": result,
            "data": self._required_dict(response, "data"),
            "unsubscribed": True,
        }
        self._subscriptions.pop(subscription_id, None)
        return parsed

    async def get_template_id(
        self,
        *,
        view: str,
        object_id: dict[str, str],
    ) -> dict[str, Any]:
        """Return the template identifier for a Viking object."""
        if not view:
            raise ValueError("view must not be empty")
        if not object_id:
            raise ValueError("object_id must not be empty")
        if not all(isinstance(value, str) and value for value in object_id.values()):
            raise ValueError("object_id values must be non-empty strings")

        response = await self.request(
            "get_template_id",
            {"view": view, "id": object_id},
        )
        result = self._required_str(response, "r")
        if result != "p":
            raise VikingProtocolError(
                "get_template_id returned an unexpected result; expected r='p'"
            )
        data = self._required_dict(response, "data")
        template_id = self._required_str(data, "template_id")
        return {
            "type": self._required_str(response, "type"),
            "eid": self._required_str(response, "eid"),
            "ts": self._required_int(response, "ts"),
            "r": result,
            "result": result,
            "data": data,
            "template_id": template_id,
        }

    async def get_template_by_id(self, *, template_id: str) -> dict[str, Any]:
        """Return a complete Viking template without filtering dynamic fields."""
        if not template_id:
            raise ValueError("template_id must not be empty")

        response = await self.request(
            "get_template_by_id",
            {"template_id": template_id},
        )
        result = self._required_str(response, "r")
        if result != "p":
            raise VikingProtocolError(
                "get_template_by_id returned an unexpected result; expected r='p'"
            )
        data = self._required_dict(response, "data")
        template = self._required_dict(data, "template")
        template_fields = self._required_dict(template, "template_fields")
        returned_template_id = self._required_str(template, "template_id")
        return {
            "type": self._required_str(response, "type"),
            "eid": self._required_str(response, "eid"),
            "ts": self._required_int(response, "ts"),
            "r": result,
            "result": result,
            "data": data,
            "requested_template_id": template_id,
            "template_id": returned_template_id,
            "template": template,
            "template_fields": template_fields,
        }

    async def get_portfolio_template(
        self,
        *,
        robot_id: str,
        portfolio: str,
    ) -> dict[str, Any]:
        """Resolve and return the complete template for a portfolio."""
        if not robot_id:
            raise ValueError("robot_id must not be empty")
        if not portfolio:
            raise ValueError("portfolio must not be empty")

        template_id_response = await self.get_template_id(
            view="portfolio",
            object_id={"r_id": robot_id, "p_id": portfolio},
        )
        requested_template_id = template_id_response["template_id"]
        template_response = await self.get_template_by_id(
            template_id=requested_template_id
        )
        return {
            "robot_id": robot_id,
            "portfolio": portfolio,
            "template_id": requested_template_id,
            "template": template_response["template"],
            "template_fields": template_response["template_fields"],
            "get_template_id_response": template_id_response,
            "get_template_by_id_response": {
                "type": template_response["type"],
                "eid": template_response["eid"],
                "ts": template_response["ts"],
                "r": template_response["r"],
                "result": template_response["result"],
                "returned_template_id": template_response["template_id"],
            },
        }

    async def get_current_portfolio_data(
        self,
        *,
        robot_id: str,
        portfolio: str,
    ) -> dict[str, Any]:
        """Return one complete current portfolio snapshot and close its subscription."""
        snapshot = await self.subscribe_portfolio(
            robot_id=robot_id,
            portfolio=portfolio,
        )
        subscription_id = snapshot["subscription_id"]
        try:
            unsubscribe_response = await self.unsubscribe_portfolio(subscription_id)
        except BaseException:
            self._subscriptions.pop(subscription_id, None)
            await self.close()
            raise
        return {
            **snapshot,
            "active": False,
            "unsubscribed": True,
            "unsubscribe_response": unsubscribe_response,
        }

    async def subscribe_portfolio(
        self,
        *,
        robot_id: str,
        portfolio: str,
    ) -> dict[str, Any]:
        """Subscribe to a portfolio and return its complete initial snapshot."""
        if not robot_id:
            raise ValueError("robot_id must not be empty")
        if not portfolio:
            raise ValueError("portfolio must not be empty")

        response = await self._subscribe(
            "portfolio.subscribe",
            {"r_id": robot_id, "p_id": portfolio},
        )
        subscription_id = self._required_str(response, "eid")
        try:
            event = self._parse_portfolio_subscription_event(
                response,
                subscription_id=subscription_id,
                expected_robot_id=robot_id,
                expected_portfolio=portfolio,
                allowed_results={"s"},
                require_complete_snapshot=True,
            )
            return {
                "subscription_id": subscription_id,
                "active": True,
                **event,
            }
        except BaseException:
            self._subscriptions.pop(subscription_id, None)
            await self.close()
            raise

    async def get_portfolio_updates(
        self,
        subscription_id: str,
        *,
        wait_seconds: float = 0,
        max_events: int = 100,
    ) -> dict[str, Any]:
        """Return buffered snapshots and updates for an active portfolio subscription."""
        subscription = self._subscriptions.get(subscription_id)
        if subscription is None or subscription.message_type != "portfolio.subscribe":
            raise ValueError("Unknown or inactive portfolio subscription_id")
        if subscription.overflowed:
            raise VikingProtocolError(
                "Portfolio subscription buffer overflowed and events were lost; "
                "unsubscribe and create a new subscription"
            )
        if not 0 <= wait_seconds <= 30:
            raise ValueError("wait_seconds must be in range 0..30")
        if not 1 <= max_events <= 500:
            raise ValueError("max_events must be in range 1..500")

        request_data = subscription.request_data or {}
        expected_robot_id = request_data.get("r_id")
        expected_portfolio = request_data.get("p_id")
        if not isinstance(expected_robot_id, str) or not isinstance(expected_portfolio, str):
            raise VikingProtocolError("Portfolio subscription metadata is missing r_id or p_id")

        messages: list[dict[str, Any]] = []
        if wait_seconds and subscription.queue.empty():
            with contextlib.suppress(TimeoutError):
                messages.append(
                    await asyncio.wait_for(subscription.queue.get(), timeout=wait_seconds)
                )
        while len(messages) < max_events:
            try:
                messages.append(subscription.queue.get_nowait())
            except asyncio.QueueEmpty:
                break

        events = []
        for message in messages:
            self._validate_response_identity(
                message,
                expected_type="portfolio.subscribe",
                expected_eid=subscription_id,
            )
            if message.get("r") == "e":
                self._subscriptions.pop(subscription_id, None)
                self._raise_api_error(message)
            event = self._parse_portfolio_subscription_event(
                message,
                subscription_id=subscription_id,
                expected_robot_id=expected_robot_id,
                expected_portfolio=expected_portfolio,
                allowed_results={"s", "u"},
                require_complete_snapshot=message.get("r") == "s",
            )
            events.append(event)
            if event["deleted"]:
                self._subscriptions.pop(subscription_id, None)
                break

        active = subscription_id in self._subscriptions
        return {
            "subscription_id": subscription_id,
            "event_count": len(events),
            "events": events,
            "active": active,
            "more_available": active and not subscription.queue.empty(),
        }

    async def unsubscribe_portfolio(self, subscription_id: str) -> dict[str, Any]:
        """Unsubscribe from portfolio updates and return the full acknowledgement."""
        subscription = self._subscriptions.get(subscription_id)
        if subscription is None or subscription.message_type != "portfolio.subscribe":
            raise ValueError("Unknown or inactive portfolio subscription_id")
        response = await self.request(
            "portfolio.unsubscribe",
            {"sub_eid": subscription_id},
        )
        self._validate_response_identity(
            response,
            expected_type="portfolio.unsubscribe",
        )
        result = self._required_str(response, "r")
        if result != "p":
            raise VikingProtocolError(
                "portfolio.unsubscribe returned an unexpected result; expected r='p'"
            )
        parsed = {
            "subscription_id": subscription_id,
            "type": self._required_str(response, "type"),
            "eid": self._required_str(response, "eid"),
            "ts": self._required_int(response, "ts"),
            "r": result,
            "result": result,
            "data": self._required_dict(response, "data"),
            "unsubscribed": True,
        }
        self._subscriptions.pop(subscription_id, None)
        return parsed

    async def get_portfolio_history(
        self,
        *,
        robot_id: str,
        portfolio: str,
        key: str,
        aggregation: str,
        start_ms: int,
        end_ms: int,
        max_points: int,
    ) -> list[dict[str, Any]]:
        response = await self.request(
            "portfolio_history.get_history",
            {
                "r_id": robot_id,
                "p_id": portfolio,
                "key": key,
                "aggr": aggregation,
                "mint": start_ms,
                "maxt": end_ms,
                "lim": min(100_000, max_points),
            },
        )
        points_by_time = self._extract_points(response, start_ms, end_ms)

        while points_by_time:
            earliest = min(points_by_time)
            if earliest <= start_ms or len(points_by_time) >= max_points:
                break

            previous = await self.request(
                "portfolio_history.get_previous",
                {
                    "r_id": robot_id,
                    "p_id": portfolio,
                    "key": key,
                    "aggr": aggregation,
                    "mt": earliest,
                    "lim": min(1_000, max_points - len(points_by_time)),
                },
            )
            older = self._extract_points(previous, start_ms, min(end_ms, earliest - 1))
            if not older or min(older) >= earliest:
                break
            points_by_time.update(older)

        if len(points_by_time) >= max_points and min(points_by_time) > start_ms:
            raise VikingAPIError(
                f"Field '{key}' exceeded MAX_POINTS_PER_FIELD={max_points}. "
                "Use a larger aggregation period or a shorter date range."
            )

        return [{"dt": timestamp, "v": points_by_time[timestamp]} for timestamp in sorted(points_by_time)]

    async def _ensure_connected(self) -> None:
        if self.connected:
            return

        async with self._connect_lock:
            if self.connected:
                return
            logger.info("Connecting to Viking WebSocket API")
            ws = await connect(
                self.settings.viking_ws_url,
                ping_interval=20,
                ping_timeout=10,
                close_timeout=5,
                max_size=None,
            )
            self._ws = ws
            self._reader_task = asyncio.create_task(self._reader_loop(ws))
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(ws))

            try:
                await self._request_connected(
                    "authorization_key",
                    {
                        "email": self.credentials.email,
                        "key": self.credentials.api_key,
                        "role": self.credentials.role,
                        "group": 0.1,
                        "compress": True,
                    },
                    timeout=self.settings.viking_request_timeout_seconds,
                )
            except Exception:
                await self.close()
                raise
            logger.info("Viking WebSocket authorization succeeded")

    async def _request_connected(
        self,
        message_type: str,
        data: dict[str, Any],
        *,
        timeout: float,
    ) -> dict[str, Any]:
        ws = self._ws
        if ws is None or ws.state is not State.OPEN:
            raise ConnectionError("Viking WebSocket is not connected")

        eid = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[eid] = future
        payload = {"type": message_type, "data": data, "eid": eid}

        try:
            async with self._send_lock:
                await ws.send(json.dumps(payload, separators=(",", ":")))
            response = await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._pending.pop(eid, None)

        try:
            self._validate_response_identity(
                response,
                expected_type=message_type,
                expected_eid=eid,
            )
            result = self._required_str(response, "r")
        except VikingProtocolError:
            await self.close()
            raise
        if result == "e":
            try:
                self._raise_api_error(response)
            except VikingProtocolError:
                await self.close()
                raise
        return response

    async def _subscribe(
        self,
        message_type: str,
        data: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        await self._ensure_connected()
        ws = self._ws
        if ws is None or ws.state is not State.OPEN:
            raise ConnectionError("Viking WebSocket is not connected")

        eid = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[eid] = future
        self._subscriptions[eid] = _Subscription(
            message_type,
            asyncio.Queue(maxsize=1_000),
            request_data=dict(data),
        )
        payload = {"type": message_type, "data": data, "eid": eid}
        try:
            async with self._send_lock:
                await ws.send(json.dumps(payload, separators=(",", ":")))
            response = await asyncio.wait_for(
                future,
                timeout=timeout or self.settings.viking_request_timeout_seconds,
            )
        except BaseException:
            self._subscriptions.pop(eid, None)
            await self.close()
            raise
        finally:
            self._pending.pop(eid, None)

        try:
            self._validate_response_identity(
                response,
                expected_type=message_type,
                expected_eid=eid,
            )
            result = self._required_str(response, "r")
        except VikingProtocolError:
            self._subscriptions.pop(eid, None)
            await self.close()
            raise
        if result == "e":
            self._subscriptions.pop(eid, None)
            try:
                self._raise_api_error(response)
            except VikingProtocolError:
                await self.close()
                raise
        return response

    async def _reader_loop(self, ws: ClientConnection) -> None:
        failure: BaseException = ConnectionError("Viking WebSocket reader stopped")
        try:
            async for raw_message in ws:
                if raw_message == "7":
                    continue
                for message in self.decode_messages(raw_message):
                    eid = message.get("eid")
                    future = self._pending.get(str(eid)) if eid is not None else None
                    if future is not None and not future.done():
                        future.set_result(message)
                        continue
                    subscription = (
                        self._subscriptions.get(str(eid)) if eid is not None else None
                    )
                    if subscription is not None:
                        if subscription.queue.full():
                            subscription.queue.get_nowait()
                            subscription.overflowed = True
                            logger.warning(
                                "Dropped oldest buffered event for subscription %s", eid
                            )
                        subscription.queue.put_nowait(message)
        except asyncio.CancelledError:
            failure = ConnectionError("Viking WebSocket reader cancelled")
            raise
        except Exception as exc:
            failure = exc
            logger.warning("Viking WebSocket reader failed: %s", type(exc).__name__)
        finally:
            if self._ws is ws:
                self._ws = None
            self._fail_pending(failure)
            self._subscriptions.clear()

    async def _heartbeat_loop(self, ws: ClientConnection) -> None:
        try:
            while ws.state is State.OPEN:
                await asyncio.sleep(3)
                async with self._send_lock:
                    await ws.send("7")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("Viking heartbeat stopped", exc_info=True)

    def _fail_pending(self, exc: BaseException) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()

    @classmethod
    def _parse_portfolio_list_data(
        cls, response: dict[str, Any]
    ) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
        data = cls._required_dict(response, "data")
        return (
            cls._parse_portfolio_rows(data.get("portfolios_add", []), "portfolios_add"),
            cls._parse_portfolio_rows(data.get("portfolios_del", []), "portfolios_del"),
        )

    @staticmethod
    def _parse_portfolio_rows(value: Any, field_name: str) -> list[dict[str, str]]:
        if not isinstance(value, list):
            raise VikingProtocolError(f"{field_name} must be an array")
        result = []
        for index, row in enumerate(value):
            if not isinstance(row, list) or len(row) != 3:
                raise VikingProtocolError(
                    f"{field_name}[{index}] must contain robot_id, portfolio and owner"
                )
            if not all(isinstance(item, str) for item in row):
                raise VikingProtocolError(f"{field_name}[{index}] fields must be strings")
            result.append(
                {"robot_id": row[0], "portfolio": row[1], "owner": row[2]}
            )
        return result

    @classmethod
    def _parse_portfolio_event(
        cls,
        response: dict[str, Any],
        *,
        subscription_id: str,
        allowed_results: set[str],
        require_portfolios_add: bool = False,
    ) -> dict[str, Any]:
        cls._validate_response_identity(
            response,
            expected_type="available_portfolio_list.subscribe",
            expected_eid=subscription_id,
        )
        result = cls._required_str(response, "r")
        if result not in allowed_results:
            expected = ", ".join(repr(item) for item in sorted(allowed_results))
            raise VikingProtocolError(
                f"Portfolio-list response has unexpected r={result!r}; expected {expected}"
            )

        source_data = cls._required_dict(response, "data")
        if require_portfolios_add and "portfolios_add" not in source_data:
            raise VikingProtocolError("Initial portfolio snapshot is missing portfolios_add")
        portfolios_add, portfolios_del = cls._parse_portfolio_list_data(response)
        data: dict[str, Any] = {}
        if "portfolios_add" in source_data:
            data["portfolios_add"] = portfolios_add
        if "portfolios_del" in source_data:
            data["portfolios_del"] = portfolios_del
        return {
            "type": cls._required_str(response, "type"),
            "eid": cls._required_str(response, "eid"),
            "ts": cls._required_int(response, "ts"),
            "r": result,
            "result": result,
            "data": data,
            "portfolios_add": portfolios_add,
            "portfolios_del": portfolios_del,
        }

    @classmethod
    def _parse_portfolio_subscription_event(
        cls,
        response: dict[str, Any],
        *,
        subscription_id: str,
        expected_robot_id: str,
        expected_portfolio: str,
        allowed_results: set[str],
        require_complete_snapshot: bool = False,
    ) -> dict[str, Any]:
        cls._validate_response_identity(
            response,
            expected_type="portfolio.subscribe",
            expected_eid=subscription_id,
        )
        result = cls._required_str(response, "r")
        if result not in allowed_results:
            expected = ", ".join(repr(item) for item in sorted(allowed_results))
            raise VikingProtocolError(
                f"Portfolio response has unexpected r={result!r}; expected {expected}"
            )

        source_data = cls._required_dict(response, "data")
        robot_id = cls._required_str(source_data, "r_id")
        portfolio = cls._required_str(source_data, "p_id")
        if robot_id != expected_robot_id:
            raise VikingProtocolError(
                f"Unexpected portfolio r_id {robot_id!r}; expected {expected_robot_id!r}"
            )
        if portfolio != expected_portfolio:
            raise VikingProtocolError(
                f"Unexpected portfolio p_id {portfolio!r}; expected {expected_portfolio!r}"
            )

        value = cls._required_dict(source_data, "value")
        name = cls._required_str(value, "name")
        if name != portfolio:
            raise VikingProtocolError(
                f"Portfolio snapshot name {name!r} does not match p_id {portfolio!r}"
            )

        action = value.get("__action")
        if action is not None and action != "del":
            raise VikingProtocolError("Portfolio __action must be 'del' when present")
        deleted = action == "del"

        if require_complete_snapshot and "securities" not in value:
            raise VikingProtocolError("Portfolio snapshot is missing securities")
        securities = value.get("securities")
        if securities is not None:
            if not isinstance(securities, dict):
                raise VikingProtocolError("Portfolio securities must be an object")
            for security_key, security in securities.items():
                if not isinstance(security, dict):
                    raise VikingProtocolError(
                        f"Portfolio security {security_key!r} must be an object"
                    )
                sec_key = cls._required_str(security, "sec_key")
                if sec_key != security_key:
                    raise VikingProtocolError(
                        f"Portfolio security key {security_key!r} does not match "
                        f"sec_key {sec_key!r}"
                    )
                security_action = security.get("__action")
                if security_action is not None and security_action != "del":
                    raise VikingProtocolError(
                        f"Portfolio security {security_key!r} __action must be 'del'"
                    )

        data = dict(source_data)
        data["value"] = dict(value)
        return {
            "type": cls._required_str(response, "type"),
            "eid": cls._required_str(response, "eid"),
            "ts": cls._required_int(response, "ts"),
            "r": result,
            "result": result,
            "data": data,
            "robot_id": robot_id,
            "portfolio": portfolio,
            "value": data["value"],
            "deleted": deleted,
        }

    @classmethod
    def _validate_response_identity(
        cls,
        response: dict[str, Any],
        *,
        expected_type: str,
        expected_eid: str | None = None,
    ) -> None:
        response_type = cls._required_str(response, "type")
        if response_type != expected_type:
            raise VikingProtocolError(
                f"Unexpected response type {response_type!r}; expected {expected_type!r}"
            )
        if expected_eid is not None:
            response_eid = cls._required_str(response, "eid")
            if response_eid != expected_eid:
                raise VikingProtocolError(
                    f"Unexpected response eid {response_eid!r}; expected {expected_eid!r}"
                )

    @classmethod
    def _raise_api_error(cls, response: dict[str, Any]) -> None:
        result = cls._required_str(response, "r")
        if result != "e":
            raise VikingProtocolError(
                f"Cannot parse a non-error response as an API error: r={result!r}"
            )
        cls._required_int(response, "ts")
        error = cls._required_dict(response, "data")
        raise VikingAPIError(
            cls._required_str(error, "msg"),
            code=cls._required_int(error, "code"),
            response=response,
        )

    @staticmethod
    def _required_dict(value: dict[str, Any], key: str) -> dict[str, Any]:
        result = value.get(key)
        if not isinstance(result, dict):
            raise VikingProtocolError(f"Response field '{key}' must be an object")
        return result

    @staticmethod
    def _required_str(value: dict[str, Any], key: str) -> str:
        result = value.get(key)
        if not isinstance(result, str):
            raise VikingProtocolError(f"Response field '{key}' must be a string")
        return result

    @staticmethod
    def _required_int(value: dict[str, Any], key: str) -> int:
        result = value.get(key)
        if not isinstance(result, int) or isinstance(result, bool):
            raise VikingProtocolError(f"Response field '{key}' must be an integer")
        return result

    @staticmethod
    def decode_messages(raw_message: str | bytes) -> list[dict[str, Any]]:
        if isinstance(raw_message, bytes):
            raw_message = zlib.decompress(raw_message, wbits=15).decode("utf-8")
        decoded = json.loads(raw_message)
        messages: Iterable[Any] = decoded if isinstance(decoded, list) else [decoded]
        return [item for item in messages if isinstance(item, dict)]

    @staticmethod
    def _extract_points(
        response: dict[str, Any],
        start_ms: int,
        end_ms: int,
    ) -> dict[int, Any]:
        result: dict[int, Any] = {}
        values = response.get("data", {}).get("values", [])
        for point in values:
            try:
                timestamp = int(point["dt"])
                value = point["v"]
            except (KeyError, TypeError, ValueError):
                continue
            if start_ms <= timestamp <= end_ms:
                result[timestamp] = value
        return result
