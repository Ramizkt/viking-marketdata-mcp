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

    async def list_available_portfolios_basic(self) -> list[dict[str, Any]]:
        """Return accessible portfolio identities without the history lookup."""
        snapshot = await self.subscribe_available_portfolios()
        rows = [dict(item) for item in snapshot["portfolios_add"]]
        try:
            await self.unsubscribe_available_portfolios(snapshot["subscription_id"])
        except Exception:
            logger.warning("Could not unsubscribe from available portfolio list", exc_info=True)
            self._subscriptions.pop(snapshot["subscription_id"], None)
            await self.close()
        rows.sort(key=lambda item: (item["robot_id"], item["portfolio"]))
        return rows

    async def list_portfolios(self) -> list[dict[str, Any]]:
        """Return all accessible portfolios and whether history is enabled."""
        all_rows = await self.list_available_portfolios_basic()
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
                raise VikingProtocolError("Initial portfolio snapshot unexpectedly contains portfolios_del")
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
                messages.append(await asyncio.wait_for(subscription.queue.get(), timeout=wait_seconds))
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
            raise VikingProtocolError("get_template_id returned an unexpected result; expected r='p'")
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
            raise VikingProtocolError("get_template_by_id returned an unexpected result; expected r='p'")
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
        template_response = await self.get_template_by_id(template_id=requested_template_id)
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

    @staticmethod
    def _collect_robot_state(value: dict[str, Any]) -> dict[str, Any]:
        """Return the robot-level part of ``robot.subscribe`` ``value``.

        ``value.re`` is dropped because it is per portfolio and is already presented by
        ``portfolio_statuses``. Every other key is kept verbatim, including ones the
        platform may add later.
        """
        return {key: item for key, item in value.items() if key != "re"}

    async def get_robot_portfolio_summary(self, *, robot_id: str) -> dict[str, Any]:
        """Return all portfolio trading flags from one robot.subscribe snapshot."""
        if not robot_id:
            raise ValueError("robot_id must not be empty")
        response = await self._subscribe("robot.subscribe", {"r_id": robot_id})
        subscription_id = self._required_str(response, "eid")
        try:
            self._validate_response_identity(
                response, expected_type="robot.subscribe", expected_eid=subscription_id
            )
            if self._required_str(response, "r") != "s":
                raise VikingProtocolError("robot.subscribe returned an unexpected result; expected r='s'")
            data = self._required_dict(response, "data")
            if self._required_str(data, "r_id") != robot_id:
                raise VikingProtocolError("Unexpected robot.subscribe r_id")
            value = self._required_dict(data, "value")
            all_portfolios = self._required_int(value, "p_a")
            disabled_portfolios = self._required_int(value, "p_d")
            expired_portfolios = self._required_int(value, "p_e")
            trading_status = self._required_int(value, "tr")
            if all_portfolios < 0:
                raise VikingProtocolError("robot.subscribe p_a must be non-negative")
            if not 0 <= disabled_portfolios <= all_portfolios:
                raise VikingProtocolError("robot.subscribe p_d must be in range 0..p_a")
            if expired_portfolios < 0:
                raise VikingProtocolError("robot.subscribe p_e must be non-negative")
            if trading_status not in {0, 2, 3}:
                raise VikingProtocolError("robot.subscribe tr must be 0, 2 or 3")
            raw_statuses = value.get("re")
            if not isinstance(raw_statuses, list):
                raise VikingProtocolError("robot.subscribe value.re must be an array")
            statuses = []
            for index, row in enumerate(raw_statuses):
                if not isinstance(row, dict):
                    raise VikingProtocolError(f"robot.subscribe value.re[{index}] must be an object")
                name = self._required_str(row, "n")
                re_flag = row.get("re")
                free_flag = row.get("f")
                if not isinstance(re_flag, bool):
                    raise VikingProtocolError(f"robot.subscribe value.re[{index}].re must be boolean")
                if not isinstance(free_flag, bool):
                    raise VikingProtocolError(f"robot.subscribe value.re[{index}].f must be boolean")
                statuses.append(
                    {
                        "portfolio": name,
                        "trading": re_flag,
                        "status": "trading" if re_flag else "not_trading",
                        "re": re_flag,
                        "f": free_flag,
                    }
                )
            robot_state = self._collect_robot_state(value)
        except BaseException:
            self._subscriptions.pop(subscription_id, None)
            await self.close()
            raise
        try:
            await self._unsubscribe_log_subscription(
                subscription_id,
                expected_subscribe_type="robot.subscribe",
                unsubscribe_type="robot.unsubscribe",
            )
        except BaseException:
            self._subscriptions.pop(subscription_id, None)
            await self.close()
            raise
        trading_count = sum(1 for item in statuses if item["trading"])
        return {
            "robot_id": robot_id,
            "all_portfolios": all_portfolios,
            "disabled_portfolios": disabled_portfolios,
            "expired_portfolios": expired_portfolios,
            "robot_trading_status": trading_status,
            "robot_trading": False if trading_status == 0 else True if trading_status == 2 else None,
            "robot_state": robot_state,
            "portfolio_statuses": statuses,
            "portfolio_status_count": len(statuses),
            "trading_portfolios": trading_count,
            "not_trading_portfolios": len(statuses) - trading_count,
            "subscription_closed": True,
        }

    async def get_current_portfolio_data_many(
        self,
        *,
        robot_id: str,
        portfolios: list[str],
    ) -> dict[str, Any]:
        """Read many portfolio snapshots using Viking request groups of at most 50 messages."""
        if not robot_id:
            raise ValueError("robot_id must not be empty")
        if len(portfolios) > 5_000:
            raise ValueError("portfolios must contain at most 5000 names")
        if any(not isinstance(portfolio, str) or not portfolio for portfolio in portfolios):
            raise ValueError("portfolio names must be non-empty strings")
        if len(set(portfolios)) != len(portfolios):
            raise ValueError("portfolio names must be unique")
        if not portfolios:
            return {
                "robot_id": robot_id,
                "item_count": 0,
                "items": [],
                "group_size": 50,
                "cleanup_reconnected": False,
            }

        subscribe_requests = [
            ("portfolio.subscribe", {"r_id": robot_id, "p_id": portfolio}) for portfolio in portfolios
        ]
        grouped = await self._grouped_exchange(
            subscribe_requests,
            register_subscriptions=True,
        )

        items: list[dict[str, Any]] = []
        active_subscriptions: list[tuple[str, str]] = []
        try:
            for portfolio, (subscription_id, outcome) in zip(portfolios, grouped, strict=True):
                if isinstance(outcome, VikingAPIError):
                    item: dict[str, Any] = {
                        "portfolio": portfolio,
                        "ok": False,
                        "error_type": "VikingAPIError",
                        "message": str(outcome),
                    }
                    if outcome.code is not None:
                        item["code"] = outcome.code
                    items.append(item)
                    continue
                event = self._parse_portfolio_subscription_event(
                    outcome,
                    subscription_id=subscription_id,
                    expected_robot_id=robot_id,
                    expected_portfolio=portfolio,
                    allowed_results={"s"},
                    require_complete_snapshot=True,
                )
                active_subscriptions.append((portfolio, subscription_id))
                items.append(
                    {
                        "portfolio": portfolio,
                        "ok": True,
                        "value": event["value"],
                    }
                )
        except VikingProtocolError:
            await self.close()
            raise

        cleanup_reconnected = False
        if active_subscriptions:
            try:
                unsubscribe_results = await self._grouped_exchange(
                    [
                        ("portfolio.unsubscribe", {"sub_eid": subscription_id})
                        for _, subscription_id in active_subscriptions
                    ],
                    register_subscriptions=False,
                )
                cleanup_failed = False
                for (_, subscription_id), (_, outcome) in zip(
                    active_subscriptions, unsubscribe_results, strict=True
                ):
                    if isinstance(outcome, VikingAPIError):
                        cleanup_failed = True
                        continue
                    result = self._required_str(outcome, "r")
                    if result != "p":
                        await self.close()
                        raise VikingProtocolError(
                            "portfolio.unsubscribe returned an unexpected result; expected r='p'"
                        )
                    self._subscriptions.pop(subscription_id, None)
                if cleanup_failed:
                    cleanup_reconnected = True
                    await self.close()
            except (TimeoutError, ConnectionError, ConnectionClosed):
                cleanup_reconnected = True
                await self.close()

        return {
            "robot_id": robot_id,
            "item_count": len(items),
            "items": items,
            "group_size": 50,
            "cleanup_reconnected": cleanup_reconnected,
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
                messages.append(await asyncio.wait_for(subscription.queue.get(), timeout=wait_seconds))
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
            raise VikingProtocolError("portfolio.unsubscribe returned an unexpected result; expected r='p'")
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

    async def subscribe_portfolio_logs(
        self,
        *,
        robot_id: str,
        portfolio: str,
    ) -> dict[str, Any]:
        """Subscribe to portfolio logs and return the initial log snapshot."""
        if not robot_id:
            raise ValueError("robot_id must not be empty")
        if not portfolio:
            raise ValueError("portfolio must not be empty")

        response = await self._subscribe(
            "portfolio_logs.subscribe",
            {"r_id": robot_id, "p_id": portfolio},
        )
        subscription_id = self._required_str(response, "eid")
        try:
            event = self._parse_log_subscription_event(
                response,
                subscription_id=subscription_id,
                expected_type="portfolio_logs.subscribe",
                expected_robot_id=robot_id,
                expected_portfolio=portfolio,
                allowed_results={"s"},
                require_snapshot=True,
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

    async def get_portfolio_log_updates(
        self,
        subscription_id: str,
        *,
        wait_seconds: float = 0,
        max_events: int = 100,
    ) -> dict[str, Any]:
        """Return buffered updates for an active portfolio-log subscription."""
        return await self._get_log_updates(
            subscription_id,
            expected_type="portfolio_logs.subscribe",
            wait_seconds=wait_seconds,
            max_events=max_events,
        )

    async def unsubscribe_portfolio_logs(self, subscription_id: str) -> dict[str, Any]:
        """Unsubscribe from portfolio logs."""
        return await self._unsubscribe_log_subscription(
            subscription_id,
            expected_subscribe_type="portfolio_logs.subscribe",
            unsubscribe_type="portfolio_logs.unsubscribe",
        )

    async def subscribe_robot_logs(self, *, robot_id: str) -> dict[str, Any]:
        """Subscribe to robot logs and return the initial log snapshot."""
        if not robot_id:
            raise ValueError("robot_id must not be empty")

        response = await self._subscribe(
            "robot_logs.subscribe",
            {"r_id": robot_id},
        )
        subscription_id = self._required_str(response, "eid")
        try:
            event = self._parse_log_subscription_event(
                response,
                subscription_id=subscription_id,
                expected_type="robot_logs.subscribe",
                expected_robot_id=robot_id,
                expected_portfolio=None,
                allowed_results={"s"},
                require_snapshot=True,
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

    async def get_robot_log_updates(
        self,
        subscription_id: str,
        *,
        wait_seconds: float = 0,
        max_events: int = 100,
    ) -> dict[str, Any]:
        """Return buffered updates for an active robot-log subscription."""
        return await self._get_log_updates(
            subscription_id,
            expected_type="robot_logs.subscribe",
            wait_seconds=wait_seconds,
            max_events=max_events,
        )

    async def unsubscribe_robot_logs(self, subscription_id: str) -> dict[str, Any]:
        """Unsubscribe from robot logs."""
        return await self._unsubscribe_log_subscription(
            subscription_id,
            expected_subscribe_type="robot_logs.subscribe",
            unsubscribe_type="robot_logs.unsubscribe",
        )

    async def get_robot_log_history(
        self,
        *,
        robot_id: str,
        mint_ns: str,
        maxt_ns: str,
        message_filter: str | None = None,
        limit: int = 100_000,
    ) -> dict[str, Any]:
        """Return robot logs received between two epoch-nanosecond bounds."""
        if not robot_id:
            raise ValueError("robot_id must not be empty")
        self._validate_epoch_nsec_bound(mint_ns, "mint_ns")
        self._validate_epoch_nsec_bound(maxt_ns, "maxt_ns")
        if int(mint_ns) >= int(maxt_ns):
            raise ValueError("mint_ns must be earlier than maxt_ns")
        if message_filter is not None and len(message_filter) > 256:
            raise ValueError("message_filter must not exceed 256 characters")
        if not 1 <= limit <= 100_000:
            raise ValueError("limit must be in range 1..100000")

        request_data: dict[str, Any] = {
            "r_id": robot_id,
            "mint": mint_ns,
            "maxt": maxt_ns,
            "lim": limit,
        }
        if message_filter is not None:
            request_data["msg"] = message_filter

        response = await self.request("robot_logs.get_history", request_data)
        result = self._required_str(response, "r")
        if result != "p":
            raise VikingProtocolError("robot_logs.get_history returned an unexpected result; expected r='p'")
        data = self._required_dict(response, "data")
        logs = self._parse_log_rows(
            data.get("values"),
            expected_robot_id=robot_id,
            expected_portfolio=None,
            require_robot_time=False,
            require_row_robot_id=True,
            require_name=True,
        )
        normalized_data = dict(data)
        normalized_data["values"] = logs
        return {
            "type": self._required_str(response, "type"),
            "eid": self._required_str(response, "eid"),
            "ts": self._required_int(response, "ts"),
            "r": result,
            "result": result,
            "data": normalized_data,
            "robot_id": robot_id,
            "mint": mint_ns,
            "maxt": maxt_ns,
            "message_filter": message_filter,
            "limit": limit,
            "log_count": len(logs),
            "logs": logs,
        }

    async def get_messages_history(
        self,
        *,
        mint_ms: int,
        maxt_ms: int,
        read: bool = False,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Return non-suppressible platform messages in an inclusive epoch-millisecond range.

        Calls ``messages.get_history`` (api.md 11.13.4). Messages are account-level: the
        request has no robot or portfolio identifiers. ``msg`` doubles as the unique id.
        """
        self._validate_epoch_msec_bound(mint_ms, "mint_ms")
        self._validate_epoch_msec_bound(maxt_ms, "maxt_ms")
        if mint_ms > maxt_ms:
            raise ValueError("mint_ms must not be later than maxt_ms")
        if not 1 <= limit <= 100:
            raise ValueError("limit must be in range 1..100")

        request_data: dict[str, Any] = {
            "mint": mint_ms,
            "maxt": maxt_ms,
            "lim": limit,
        }
        if read:
            request_data["read"] = True

        response = await self.request("messages.get_history", request_data)
        result = self._required_str(response, "r")
        if result != "p":
            raise VikingProtocolError("messages.get_history returned an unexpected result; expected r='p'")
        data = self._required_dict(response, "data")
        messages = self._parse_message_rows(data.get("values"))
        normalized_data = dict(data)
        normalized_data["values"] = messages
        count = data.get("count")
        if count is not None and (not isinstance(count, int) or isinstance(count, bool)):
            raise VikingProtocolError("Response field 'count' must be an integer when present")
        return {
            "type": self._required_str(response, "type"),
            "eid": self._required_str(response, "eid"),
            "ts": self._required_int(response, "ts"),
            "r": result,
            "result": result,
            "data": normalized_data,
            "mint": mint_ms,
            "maxt": maxt_ms,
            "read": read,
            "limit": limit,
            "count": count,
            "message_count": len(messages),
            "messages": messages,
        }

    def _parse_message_rows(self, values: Any) -> list[dict[str, Any]]:
        """Validate ``messages.*`` rows: ``msg`` is required, ``st``/``dt`` are kept as received."""
        if values is None:
            return []
        if not isinstance(values, list):
            raise VikingProtocolError("Response field 'values' must be an array")
        rows: list[dict[str, Any]] = []
        for item in values:
            if not isinstance(item, dict):
                raise VikingProtocolError("Each message must be an object")
            msg = item.get("msg")
            if not isinstance(msg, str) or not msg:
                raise VikingProtocolError("Message field 'msg' must be a non-empty string")
            state = item.get("st")
            if state is not None and (isinstance(state, bool) or state not in (0, 1)):
                raise VikingProtocolError("Message field 'st' must be 0 or 1 when present")
            dt = item.get("dt")
            if dt is not None and not (
                (isinstance(dt, int) and not isinstance(dt, bool) and dt >= 0)
                or (isinstance(dt, str) and dt.isdigit())
            ):
                raise VikingProtocolError(
                    "Message field 'dt' must be a non-negative epoch_msec integer or digit string"
                )
            rows.append(dict(item))
        return rows

    async def subscribe_portfolio_deals(self, *, robot_id: str, portfolio: str) -> dict[str, Any]:
        """Subscribe to portfolio deals and return the initial snapshot."""
        if not robot_id:
            raise ValueError("robot_id must not be empty")
        if not portfolio:
            raise ValueError("portfolio must not be empty")
        response = await self._subscribe("portfolio_deals.subscribe", {"r_id": robot_id, "p_id": portfolio})
        subscription_id = self._required_str(response, "eid")
        try:
            event = self._parse_deal_event(
                response,
                subscription_id=subscription_id,
                expected_robot_id=robot_id,
                expected_portfolio=portfolio,
                allowed_results={"s"},
                require_max_time=True,
            )
            return {"subscription_id": subscription_id, "active": True, **event}
        except BaseException:
            self._subscriptions.pop(subscription_id, None)
            await self.close()
            raise

    async def get_portfolio_deal_updates(
        self,
        subscription_id: str,
        *,
        wait_seconds: float = 0,
        max_events: int = 100,
    ) -> dict[str, Any]:
        """Return buffered updates for an active portfolio-deal subscription."""
        subscription = self._subscriptions.get(subscription_id)
        expected_type = "portfolio_deals.subscribe"
        if subscription is None or subscription.message_type != expected_type:
            raise ValueError("Unknown or inactive portfolio-deals subscription_id")
        if subscription.overflowed:
            raise VikingProtocolError(
                "Portfolio-deals subscription buffer overflowed and events were lost; "
                "unsubscribe and create a new subscription"
            )
        if not 0 <= wait_seconds <= 30:
            raise ValueError("wait_seconds must be in range 0..30")
        if not 1 <= max_events <= 500:
            raise ValueError("max_events must be in range 1..500")
        request_data = subscription.request_data or {}
        robot_id = request_data.get("r_id")
        portfolio = request_data.get("p_id")
        if not isinstance(robot_id, str) or not isinstance(portfolio, str):
            raise VikingProtocolError("Portfolio-deals subscription metadata is incomplete")

        messages: list[dict[str, Any]] = []
        if wait_seconds and subscription.queue.empty():
            with contextlib.suppress(TimeoutError):
                messages.append(await asyncio.wait_for(subscription.queue.get(), timeout=wait_seconds))
        while len(messages) < max_events:
            try:
                messages.append(subscription.queue.get_nowait())
            except asyncio.QueueEmpty:
                break

        events = []
        for message in messages:
            if message.get("r") == "e":
                self._subscriptions.pop(subscription_id, None)
                self._raise_api_error(message)
            try:
                events.append(
                    self._parse_deal_event(
                        message,
                        subscription_id=subscription_id,
                        expected_robot_id=robot_id,
                        expected_portfolio=portfolio,
                        allowed_results={"s", "u"},
                        require_max_time=message.get("r") == "s",
                    )
                )
            except VikingProtocolError:
                self._subscriptions.pop(subscription_id, None)
                await self.close()
                raise
        active = subscription_id in self._subscriptions
        return {
            "subscription_id": subscription_id,
            "event_count": len(events),
            "events": events,
            "active": active,
            "more_available": active and not subscription.queue.empty(),
        }

    async def unsubscribe_portfolio_deals(self, subscription_id: str) -> dict[str, Any]:
        """Unsubscribe from portfolio deals using the subscription eid."""
        return await self._unsubscribe_log_subscription(
            subscription_id,
            expected_subscribe_type="portfolio_deals.subscribe",
            unsubscribe_type="portfolio_deals.unsubscribe",
        )

    async def get_previous_portfolio_deals(
        self,
        *,
        robot_id: str,
        portfolio: str,
        before_ns: str,
        security_key: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Return deals older than the specified epoch-nanosecond timestamp."""
        self._validate_deal_request(robot_id, portfolio, security_key)
        self._validate_epoch_nsec_bound(before_ns, "before_ns")
        if not 1 <= limit <= 100:
            raise ValueError("limit must be in range 1..100")
        data: dict[str, Any] = {"r_id": robot_id, "p_id": portfolio, "mt": before_ns, "lim": limit}
        if security_key is not None:
            data["sec_key"] = security_key
        return await self._get_portfolio_deals_response(
            "portfolio_deals.get_previous", data, robot_id, portfolio, security_key=security_key, limit=limit
        )

    async def get_portfolio_deal_sec_keys(self, *, robot_id: str, portfolio: str) -> dict[str, Any]:
        """Return unique security keys from portfolio deal history."""
        self._validate_deal_request(robot_id, portfolio, None)
        response = await self.request("portfolio_deals.get_sec_keys", {"r_id": robot_id, "p_id": portfolio})
        result = self._required_str(response, "r")
        if result != "p":
            raise VikingProtocolError(
                "portfolio_deals.get_sec_keys returned an unexpected result; expected r='p'"
            )
        data = self._required_dict(response, "data")
        values = data.get("values")
        if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
            raise VikingProtocolError("Deal sec_keys response field 'values' must be an array of strings")
        return {
            "type": self._required_str(response, "type"),
            "eid": self._required_str(response, "eid"),
            "ts": self._required_int(response, "ts"),
            "r": result,
            "result": result,
            "data": {**data, "values": values},
            "robot_id": robot_id,
            "portfolio": portfolio,
            "security_count": len(values),
            "security_keys": values,
        }

    async def get_portfolio_deal_history(
        self,
        *,
        robot_id: str,
        portfolio: str,
        mint_ns: str,
        maxt_ns: str,
        security_key: str | None = None,
        limit: int = 100_000,
    ) -> dict[str, Any]:
        """Return portfolio deals inside inclusive epoch-nanosecond bounds."""
        self._validate_deal_request(robot_id, portfolio, security_key)
        self._validate_epoch_nsec_bound(mint_ns, "mint_ns")
        self._validate_epoch_nsec_bound(maxt_ns, "maxt_ns")
        if int(mint_ns) > int(maxt_ns):
            raise ValueError("mint_ns must not be later than maxt_ns")
        if not 1 <= limit <= 100_000:
            raise ValueError("limit must be in range 1..100000")
        data: dict[str, Any] = {
            "r_id": robot_id,
            "p_id": portfolio,
            "mint": mint_ns,
            "maxt": maxt_ns,
            "lim": limit,
        }
        if security_key is not None:
            data["sec_key"] = security_key
        return await self._get_portfolio_deals_response(
            "portfolio_deals.get_history", data, robot_id, portfolio, security_key=security_key, limit=limit
        )

    async def get_robot_securities(
        self,
        *,
        robot_id: str,
        reload: bool = False,
        sec_type: int | None = None,
    ) -> dict[str, Any]:
        """Return every page of securities available in a robot."""
        if not robot_id:
            raise ValueError("robot_id must not be empty")
        if not isinstance(reload, bool):
            raise ValueError("reload must be a boolean")
        if sec_type is not None and (
            isinstance(sec_type, bool) or not isinstance(sec_type, int) or sec_type < 0
        ):
            raise ValueError("sec_type must be a non-negative integer bit mask")

        request_data: dict[str, Any] = {"r_id": robot_id, "reload": reload}
        if sec_type is not None:
            request_data["sec_type"] = sec_type
        first = await self._subscribe("robot.get_securities", request_data)
        request_id = self._required_str(first, "eid")
        subscription = self._subscriptions.get(request_id)
        pages = [first]
        try:
            while self._required_bool(self._required_dict(pages[-1], "data"), "next"):
                if subscription is None:
                    raise VikingProtocolError("robot.get_securities pagination state was lost")
                pages.append(
                    await asyncio.wait_for(
                        subscription.queue.get(),
                        timeout=self.settings.viking_request_timeout_seconds,
                    )
                )
        finally:
            self._subscriptions.pop(request_id, None)

        securities: dict[str, dict[str, Any]] = {}
        for page in pages:
            self._validate_response_identity(
                page, expected_type="robot.get_securities", expected_eid=request_id
            )
            if self._required_str(page, "r") != "p":
                raise VikingProtocolError("robot.get_securities returned unexpected result")
            values = self._required_dict(self._required_dict(page, "data"), "securities")
            for key, security in values.items():
                if not isinstance(security, dict):
                    raise VikingProtocolError(f"Security {key!r} must be an object")
                if self._required_str(security, "sec_key") != key:
                    raise VikingProtocolError(f"Security key {key!r} does not match sec_key")
                securities[key] = dict(security)
        return {
            "type": "robot.get_securities",
            "eid": request_id,
            "ts": self._required_int(pages[-1], "ts"),
            "r": "p",
            "result": "p",
            "robot_id": robot_id,
            "reload": reload,
            "sec_type": sec_type,
            "page_count": len(pages),
            "security_count": len(securities),
            "securities": securities,
        }

    async def get_robot_client_codes(self, *, robot_id: str) -> dict[str, Any]:
        if not robot_id:
            raise ValueError("robot_id must not be empty")
        response = await self.request("robot.get_client_codes", {"r_id": robot_id})
        parsed = self._parse_plain_response(response, "robot.get_client_codes")
        data = parsed["data"]
        if self._required_str(data, "r_id") != robot_id:
            raise VikingProtocolError("Unexpected robot.get_client_codes r_id")
        values = data.get("values")
        if not isinstance(values, list):
            raise VikingProtocolError("robot.get_client_codes values must be an array")
        client_codes = []
        for index, item in enumerate(values):
            if not isinstance(item, dict):
                raise VikingProtocolError(f"Client code {index} must be an object")
            sec_type = self._required_int(item, "sec_type")
            label = self._required_str(item, "ll")
            client_codes.append({**item, "sec_type": sec_type, "ll": label})
        return {
            **parsed,
            "robot_id": robot_id,
            "client_code_count": len(client_codes),
            "client_codes": client_codes,
        }

    async def find_security(
        self,
        *,
        security_key: str,
        robot_id: str | None = None,
        portfolio: str | None = None,
    ) -> dict[str, Any]:
        if not security_key:
            raise ValueError("security_key must not be empty")
        if robot_id is not None and not robot_id:
            raise ValueError("robot_id must not be empty")
        if portfolio is not None and not portfolio:
            raise ValueError("portfolio must not be empty")
        request_data: dict[str, Any] = {"key": security_key}
        if robot_id is not None:
            request_data["r_id"] = robot_id
        if portfolio is not None:
            request_data["p_id"] = portfolio
        response = await self.request("robot.find_security", request_data)
        parsed = self._parse_plain_response(response, "robot.find_security")
        data = parsed["data"]
        if self._required_str(data, "key") != security_key:
            raise VikingProtocolError("Unexpected robot.find_security key")
        portfolios = data.get("portfolios")
        formulas = data.get("formulas")
        if not isinstance(portfolios, list) or not all(isinstance(item, dict) for item in portfolios):
            raise VikingProtocolError("robot.find_security portfolios must be an array")
        if not isinstance(formulas, list) or not all(isinstance(item, dict) for item in formulas):
            raise VikingProtocolError("robot.find_security formulas must be an array")
        return {
            **parsed,
            "security_key": security_key,
            "scope": {"robot_id": robot_id, "portfolio": portfolio},
            "portfolio_count": len(portfolios),
            "formula_match_count": len(formulas),
            "portfolios": [dict(item) for item in portfolios],
            "formulas": [dict(item) for item in formulas],
        }

    async def subscribe_data_connections(self, *, robot_id: str) -> dict[str, Any]:
        return await self._subscribe_connection_feed(
            "data_conn.subscribe", robot_id=robot_id, connection=None
        )

    async def get_data_connection_updates(
        self, subscription_id: str, *, wait_seconds: float = 0, max_events: int = 100
    ) -> dict[str, Any]:
        return await self._get_connection_feed_updates(
            subscription_id,
            expected_type="data_conn.subscribe",
            wait_seconds=wait_seconds,
            max_events=max_events,
        )

    async def unsubscribe_data_connections(self, subscription_id: str) -> dict[str, Any]:
        return await self._unsubscribe_log_subscription(
            subscription_id,
            expected_subscribe_type="data_conn.subscribe",
            unsubscribe_type="data_conn.unsubscribe",
        )

    async def get_all_data_connections(self, *, robot_id: str) -> dict[str, Any]:
        return await self._get_connection_list("data_conn.get_all", robot_id=robot_id)

    async def get_transaction_connection(self, *, robot_id: str, sec_type: int, name: str) -> dict[str, Any]:
        self._validate_connection_identity(robot_id, sec_type, name)
        response = await self.request(
            "trans_conn.get",
            {"r_id": robot_id, "conn": {"sec_type": sec_type, "name": name}},
        )
        parsed = self._parse_plain_response(response, "trans_conn.get")
        data = parsed["data"]
        returned_robot_id = self._required_str(data, "r_id")
        connection = self._required_dict(data, "conn")
        self._validate_returned_connection(connection, expected_sec_type=sec_type, expected_name=name)
        if returned_robot_id != robot_id:
            raise VikingProtocolError("Unexpected trans_conn.get r_id")
        return {**parsed, "robot_id": robot_id, "connection": dict(connection)}

    async def get_transaction_connection_used_securities(
        self, *, robot_id: str, sec_type: int, name: str
    ) -> dict[str, Any]:
        """Use the real wire type; the API request table incorrectly says trans_conn.get."""
        self._validate_connection_identity(robot_id, sec_type, name)
        response = await self.request(
            "trans_conn.get_used_secs",
            {"r_id": robot_id, "conn": {"sec_type": sec_type, "name": name}},
        )
        parsed = self._parse_plain_response(response, "trans_conn.get_used_secs")
        contracts = self._required_dict(parsed["data"], "contracts")
        for key, contract in contracts.items():
            if not isinstance(contract, dict):
                raise VikingProtocolError(f"Contract {key!r} must be an object")
            if self._required_str(contract, "sec_key") != key:
                raise VikingProtocolError(f"Contract key {key!r} does not match sec_key")
        return {
            **parsed,
            "robot_id": robot_id,
            "connection": {"sec_type": sec_type, "name": name},
            "contracts": {key: dict(value) for key, value in contracts.items()},
            "security_count": len(contracts),
        }

    async def subscribe_transaction_connections(self, *, robot_id: str) -> dict[str, Any]:
        return await self._subscribe_connection_feed(
            "trans_conn.subscribe", robot_id=robot_id, connection=None
        )

    async def get_transaction_connection_updates(
        self, subscription_id: str, *, wait_seconds: float = 0, max_events: int = 100
    ) -> dict[str, Any]:
        return await self._get_connection_feed_updates(
            subscription_id,
            expected_type="trans_conn.subscribe",
            wait_seconds=wait_seconds,
            max_events=max_events,
        )

    async def get_all_transaction_connections(self, *, robot_id: str) -> dict[str, Any]:
        return await self._get_connection_list("trans_conn.get_all", robot_id=robot_id)

    async def unsubscribe_transaction_connections(self, subscription_id: str) -> dict[str, Any]:
        return await self._unsubscribe_log_subscription(
            subscription_id,
            expected_subscribe_type="trans_conn.subscribe",
            unsubscribe_type="trans_conn.unsubscribe",
        )

    async def subscribe_transaction_orders(
        self, *, robot_id: str, sec_type: int, name: str
    ) -> dict[str, Any]:
        return await self._subscribe_connection_feed(
            "trans_conn_orders.subscribe",
            robot_id=robot_id,
            connection={"sec_type": sec_type, "name": name},
        )

    async def get_transaction_order_updates(
        self, subscription_id: str, *, wait_seconds: float = 0, max_events: int = 100
    ) -> dict[str, Any]:
        return await self._get_connection_feed_updates(
            subscription_id,
            expected_type="trans_conn_orders.subscribe",
            wait_seconds=wait_seconds,
            max_events=max_events,
        )

    async def unsubscribe_transaction_orders(self, subscription_id: str) -> dict[str, Any]:
        return await self._unsubscribe_log_subscription(
            subscription_id,
            expected_subscribe_type="trans_conn_orders.subscribe",
            unsubscribe_type="trans_conn_orders.unsubscribe",
        )

    async def subscribe_transaction_positions(
        self, *, robot_id: str, sec_type: int, name: str
    ) -> dict[str, Any]:
        return await self._subscribe_connection_feed(
            "trans_conn_poses.subscribe",
            robot_id=robot_id,
            connection={"sec_type": sec_type, "name": name},
        )

    async def get_transaction_position_updates(
        self, subscription_id: str, *, wait_seconds: float = 0, max_events: int = 100
    ) -> dict[str, Any]:
        return await self._get_connection_feed_updates(
            subscription_id,
            expected_type="trans_conn_poses.subscribe",
            wait_seconds=wait_seconds,
            max_events=max_events,
        )

    async def unsubscribe_transaction_positions(self, subscription_id: str) -> dict[str, Any]:
        return await self._unsubscribe_log_subscription(
            subscription_id,
            expected_subscribe_type="trans_conn_poses.subscribe",
            unsubscribe_type="trans_conn_poses.unsubscribe",
        )

    async def _get_connection_list(self, message_type: str, *, robot_id: str) -> dict[str, Any]:
        if not robot_id:
            raise ValueError("robot_id must not be empty")
        response = await self.request(message_type, {"r_id": robot_id})
        parsed = self._parse_plain_response(response, message_type)
        data = parsed["data"]
        if self._required_str(data, "r_id") != robot_id:
            raise VikingProtocolError(f"Unexpected {message_type} r_id")
        values = self._required_dict(data, "values")
        self._validate_connection_map(values)
        return {
            **parsed,
            "robot_id": robot_id,
            "connections": {key: dict(value) for key, value in values.items()},
            "connection_count": len(values),
        }

    async def _subscribe_connection_feed(
        self,
        message_type: str,
        *,
        robot_id: str,
        connection: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if not robot_id:
            raise ValueError("robot_id must not be empty")
        data: dict[str, Any] = {"r_id": robot_id}
        if connection is not None:
            self._validate_connection_identity(robot_id, connection.get("sec_type"), connection.get("name"))
            data["conn"] = connection
        response = await self._subscribe(message_type, data)
        subscription_id = self._required_str(response, "eid")
        try:
            event = self._parse_connection_event(
                response,
                expected_type=message_type,
                subscription_id=subscription_id,
                expected_robot_id=robot_id,
                expected_connection=connection,
            )
            return {"subscription_id": subscription_id, "active": True, **event}
        except BaseException:
            self._subscriptions.pop(subscription_id, None)
            await self.close()
            raise

    async def _get_connection_feed_updates(
        self,
        subscription_id: str,
        *,
        expected_type: str,
        wait_seconds: float,
        max_events: int,
    ) -> dict[str, Any]:
        subscription = self._subscriptions.get(subscription_id)
        if subscription is None or subscription.message_type != expected_type:
            raise ValueError(f"Unknown or inactive {expected_type} subscription_id")
        if subscription.overflowed:
            raise VikingProtocolError(f"{expected_type} subscription buffer overflowed and events were lost")
        if not 0 <= wait_seconds <= 30:
            raise ValueError("wait_seconds must be in range 0..30")
        if not 1 <= max_events <= 500:
            raise ValueError("max_events must be in range 1..500")
        request_data = subscription.request_data or {}
        messages: list[dict[str, Any]] = []
        if wait_seconds and subscription.queue.empty():
            with contextlib.suppress(TimeoutError):
                messages.append(await asyncio.wait_for(subscription.queue.get(), timeout=wait_seconds))
        while len(messages) < max_events:
            try:
                messages.append(subscription.queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        events = []
        for message in messages:
            if message.get("r") == "e":
                self._subscriptions.pop(subscription_id, None)
                self._raise_api_error(message)
            events.append(
                self._parse_connection_event(
                    message,
                    expected_type=expected_type,
                    subscription_id=subscription_id,
                    expected_robot_id=self._required_str(request_data, "r_id"),
                    expected_connection=request_data.get("conn"),
                )
            )
        active = subscription_id in self._subscriptions
        return {
            "subscription_id": subscription_id,
            "event_count": len(events),
            "events": events,
            "active": active,
            "more_available": active and not subscription.queue.empty(),
        }

    @classmethod
    def _parse_connection_event(
        cls,
        response: dict[str, Any],
        *,
        expected_type: str,
        subscription_id: str,
        expected_robot_id: str,
        expected_connection: dict[str, Any] | None,
    ) -> dict[str, Any]:
        cls._validate_response_identity(response, expected_type=expected_type, expected_eid=subscription_id)
        result = cls._required_str(response, "r")
        if result not in {"s", "u"}:
            raise VikingProtocolError(f"{expected_type} returned unexpected r={result!r}")
        source_data = cls._required_dict(response, "data")
        if cls._required_str(source_data, "r_id") != expected_robot_id:
            raise VikingProtocolError(f"Unexpected {expected_type} r_id")
        field = "value" if expected_connection is not None else "values"
        value = cls._required_dict(source_data, field)
        if expected_connection is None:
            cls._validate_connection_map(value)
        else:
            cls._validate_returned_connection(
                value,
                expected_sec_type=expected_connection["sec_type"],
                expected_name=expected_connection["name"],
            )
        data = dict(source_data)
        data[field] = dict(value)
        return {
            "type": expected_type,
            "eid": subscription_id,
            "ts": cls._required_int(response, "ts"),
            "r": result,
            "result": result,
            "data": data,
            "robot_id": expected_robot_id,
            field: data[field],
        }

    @classmethod
    def _parse_plain_response(cls, response: dict[str, Any], expected_type: str) -> dict[str, Any]:
        cls._validate_response_identity(response, expected_type=expected_type)
        result = cls._required_str(response, "r")
        if result != "p":
            raise VikingProtocolError(f"{expected_type} returned an unexpected result; expected r='p'")
        return {
            "type": expected_type,
            "eid": cls._required_str(response, "eid"),
            "ts": cls._required_int(response, "ts"),
            "r": result,
            "result": result,
            "data": dict(cls._required_dict(response, "data")),
        }

    @classmethod
    def _validate_connection_map(cls, values: dict[str, Any]) -> None:
        for key, connection in values.items():
            if not isinstance(connection, dict):
                raise VikingProtocolError(f"Connection {key!r} must be an object")
            sec_type = connection.get("sec_type")
            name = connection.get("name")
            cls._validate_returned_connection(connection, expected_sec_type=sec_type, expected_name=name)
            if key != f"{sec_type}_{name}":
                raise VikingProtocolError(f"Connection key {key!r} does not match sec_type + '_' + name")

    @classmethod
    def _validate_returned_connection(
        cls, connection: dict[str, Any], *, expected_sec_type: Any, expected_name: Any
    ) -> None:
        sec_type = connection.get("sec_type")
        if isinstance(sec_type, bool) or not isinstance(sec_type, int):
            raise VikingProtocolError("Connection sec_type must be an integer")
        name = cls._required_str(connection, "name")
        if sec_type != expected_sec_type or name != expected_name:
            raise VikingProtocolError("Unexpected connection identity")
        action = connection.get("__action")
        if action is not None and action != "del":
            raise VikingProtocolError("Connection __action must be 'del' when present")

    @staticmethod
    def _validate_connection_identity(robot_id: str, sec_type: Any, name: Any) -> None:
        if not robot_id:
            raise ValueError("robot_id must not be empty")
        if isinstance(sec_type, bool) or not isinstance(sec_type, int) or sec_type < 0:
            raise ValueError("sec_type must be a non-negative integer")
        if not isinstance(name, str) or not name:
            raise ValueError("name must not be empty")

    async def _get_portfolio_deals_response(
        self,
        message_type: str,
        request_data: dict[str, Any],
        robot_id: str,
        portfolio: str,
        *,
        security_key: str | None,
        limit: int,
    ) -> dict[str, Any]:
        response = await self.request(message_type, request_data)
        result = self._required_str(response, "r")
        if result != "p":
            raise VikingProtocolError(f"{message_type} returned an unexpected result; expected r='p'")
        data = self._required_dict(response, "data")
        deals = self._parse_deal_rows(
            data.get("values"),
            expected_robot_id=robot_id,
            expected_portfolio=portfolio,
            expected_security_key=security_key,
        )
        return {
            "type": self._required_str(response, "type"),
            "eid": self._required_str(response, "eid"),
            "ts": self._required_int(response, "ts"),
            "r": result,
            "result": result,
            "data": {**data, "values": deals},
            "robot_id": robot_id,
            "portfolio": portfolio,
            "security_key": security_key,
            "limit": limit,
            "deal_count": len(deals),
            "deals": deals,
        }

    @staticmethod
    def _validate_deal_request(robot_id: str, portfolio: str, security_key: str | None) -> None:
        if not robot_id:
            raise ValueError("robot_id must not be empty")
        if not portfolio:
            raise ValueError("portfolio must not be empty")
        if security_key is not None and not security_key:
            raise ValueError("security_key must not be empty")

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

    async def _grouped_exchange(
        self,
        requests: list[tuple[str, dict[str, Any]]],
        *,
        register_subscriptions: bool,
        timeout: float | None = None,
    ) -> list[tuple[str, dict[str, Any] | VikingAPIError]]:
        """Send request groups documented by Viking; one JSON list contains at most 50 messages."""
        if not requests:
            return []
        await self._ensure_connected()
        ws = self._ws
        if ws is None or ws.state is not State.OPEN:
            raise ConnectionError("Viking WebSocket is not connected")

        loop = asyncio.get_running_loop()
        records: list[
            tuple[
                str,
                str,
                dict[str, Any],
                asyncio.Future[dict[str, Any]],
                dict[str, Any],
            ]
        ] = []
        for message_type, data in requests:
            eid = uuid.uuid4().hex
            future: asyncio.Future[dict[str, Any]] = loop.create_future()
            self._pending[eid] = future
            if register_subscriptions:
                self._subscriptions[eid] = _Subscription(
                    message_type,
                    asyncio.Queue(maxsize=1_000),
                    request_data=dict(data),
                )
            payload = {"type": message_type, "data": data, "eid": eid}
            records.append((eid, message_type, data, future, payload))

        try:
            async with self._send_lock:
                payloads = [record[4] for record in records]
                for offset in range(0, len(payloads), 50):
                    group = payloads[offset : offset + 50]
                    await ws.send(json.dumps(group, separators=(",", ":")))
            responses = await asyncio.wait_for(
                asyncio.gather(*(record[3] for record in records)),
                timeout=timeout or self.settings.viking_request_timeout_seconds,
            )
        except BaseException:
            for eid, _, _, _, _ in records:
                self._pending.pop(eid, None)
                if register_subscriptions:
                    self._subscriptions.pop(eid, None)
            await self.close()
            raise
        finally:
            for eid, _, _, _, _ in records:
                self._pending.pop(eid, None)

        outcomes: list[tuple[str, dict[str, Any] | VikingAPIError]] = []
        try:
            for (eid, message_type, _, _, _), response in zip(records, responses, strict=True):
                self._validate_response_identity(
                    response,
                    expected_type=message_type,
                    expected_eid=eid,
                )
                result = self._required_str(response, "r")
                if result == "e":
                    if register_subscriptions:
                        self._subscriptions.pop(eid, None)
                    try:
                        self._raise_api_error(response)
                    except VikingAPIError as exc:
                        outcomes.append((eid, exc))
                        continue
                outcomes.append((eid, response))
        except VikingProtocolError:
            await self.close()
            raise
        return outcomes

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
                    subscription = self._subscriptions.get(str(eid)) if eid is not None else None
                    if subscription is not None:
                        if subscription.queue.full():
                            subscription.queue.get_nowait()
                            subscription.overflowed = True
                            logger.warning("Dropped oldest buffered event for subscription %s", eid)
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

    async def _get_log_updates(
        self,
        subscription_id: str,
        *,
        expected_type: str,
        wait_seconds: float,
        max_events: int,
    ) -> dict[str, Any]:
        subscription = self._subscriptions.get(subscription_id)
        if subscription is None or subscription.message_type != expected_type:
            raise ValueError(f"Unknown or inactive {expected_type} subscription_id")
        if subscription.overflowed:
            raise VikingProtocolError(
                f"{expected_type} buffer overflowed and log events were lost; "
                "unsubscribe and create a new subscription"
            )
        if not 0 <= wait_seconds <= 30:
            raise ValueError("wait_seconds must be in range 0..30")
        if not 1 <= max_events <= 500:
            raise ValueError("max_events must be in range 1..500")

        request_data = subscription.request_data or {}
        expected_robot_id = request_data.get("r_id")
        expected_portfolio = request_data.get("p_id")
        if not isinstance(expected_robot_id, str):
            raise VikingProtocolError("Log subscription metadata is missing r_id")
        if expected_type == "portfolio_logs.subscribe" and not isinstance(expected_portfolio, str):
            raise VikingProtocolError("Portfolio-log subscription metadata is missing p_id")

        messages: list[dict[str, Any]] = []
        if wait_seconds and subscription.queue.empty():
            with contextlib.suppress(TimeoutError):
                messages.append(await asyncio.wait_for(subscription.queue.get(), timeout=wait_seconds))
        while len(messages) < max_events:
            try:
                messages.append(subscription.queue.get_nowait())
            except asyncio.QueueEmpty:
                break

        events = []
        for message in messages:
            try:
                self._validate_response_identity(
                    message,
                    expected_type=expected_type,
                    expected_eid=subscription_id,
                )
            except VikingProtocolError:
                self._subscriptions.pop(subscription_id, None)
                await self.close()
                raise
            if message.get("r") == "e":
                self._subscriptions.pop(subscription_id, None)
                self._raise_api_error(message)
            try:
                event = self._parse_log_subscription_event(
                    message,
                    subscription_id=subscription_id,
                    expected_type=expected_type,
                    expected_robot_id=expected_robot_id,
                    expected_portfolio=expected_portfolio,
                    allowed_results={"s", "u"},
                    require_snapshot=message.get("r") == "s",
                )
            except VikingProtocolError:
                self._subscriptions.pop(subscription_id, None)
                await self.close()
                raise
            events.append(event)

        active = subscription_id in self._subscriptions
        return {
            "subscription_id": subscription_id,
            "event_count": len(events),
            "events": events,
            "active": active,
            "more_available": active and not subscription.queue.empty(),
        }

    async def _unsubscribe_log_subscription(
        self,
        subscription_id: str,
        *,
        expected_subscribe_type: str,
        unsubscribe_type: str,
    ) -> dict[str, Any]:
        subscription = self._subscriptions.get(subscription_id)
        if subscription is None or subscription.message_type != expected_subscribe_type:
            raise ValueError(f"Unknown or inactive {expected_subscribe_type} subscription_id")
        response = await self.request(
            unsubscribe_type,
            {"sub_eid": subscription_id},
        )
        self._validate_response_identity(response, expected_type=unsubscribe_type)
        result = self._required_str(response, "r")
        if result != "p":
            raise VikingProtocolError(f"{unsubscribe_type} returned an unexpected result; expected r='p'")
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
                raise VikingProtocolError(f"{field_name}[{index}] must contain robot_id, portfolio and owner")
            if not all(isinstance(item, str) for item in row):
                raise VikingProtocolError(f"{field_name}[{index}] fields must be strings")
            result.append({"robot_id": row[0], "portfolio": row[1], "owner": row[2]})
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
            raise VikingProtocolError(f"Portfolio response has unexpected r={result!r}; expected {expected}")

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
            raise VikingProtocolError(f"Portfolio snapshot name {name!r} does not match p_id {portfolio!r}")

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
                    raise VikingProtocolError(f"Portfolio security {security_key!r} must be an object")
                sec_key = cls._required_str(security, "sec_key")
                if sec_key != security_key:
                    raise VikingProtocolError(
                        f"Portfolio security key {security_key!r} does not match sec_key {sec_key!r}"
                    )
                security_action = security.get("__action")
                if security_action is not None and security_action != "del":
                    raise VikingProtocolError(f"Portfolio security {security_key!r} __action must be 'del'")

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
    def _parse_log_subscription_event(
        cls,
        response: dict[str, Any],
        *,
        subscription_id: str,
        expected_type: str,
        expected_robot_id: str,
        expected_portfolio: str | None,
        allowed_results: set[str],
        require_snapshot: bool,
    ) -> dict[str, Any]:
        cls._validate_response_identity(
            response,
            expected_type=expected_type,
            expected_eid=subscription_id,
        )
        result = cls._required_str(response, "r")
        if result not in allowed_results:
            expected = ", ".join(repr(item) for item in sorted(allowed_results))
            raise VikingProtocolError(
                f"{expected_type} returned unexpected r={result!r}; expected {expected}"
            )

        source_data = cls._required_dict(response, "data")
        robot_id = cls._required_str(source_data, "r_id")
        if robot_id != expected_robot_id:
            raise VikingProtocolError(f"Unexpected log r_id {robot_id!r}; expected {expected_robot_id!r}")

        portfolio: str | None = None
        if expected_portfolio is not None:
            portfolio = cls._required_str(source_data, "p_id")
            if portfolio != expected_portfolio:
                raise VikingProtocolError(
                    f"Unexpected log p_id {portfolio!r}; expected {expected_portfolio!r}"
                )

        max_time: int | str | None = None
        if require_snapshot or "mt" in source_data:
            max_time = cls._required_epoch_nsec(source_data, "mt")

        logs = cls._parse_log_rows(
            source_data.get("values"),
            expected_robot_id=expected_robot_id,
            expected_portfolio=expected_portfolio,
            require_robot_time=True,
            require_row_robot_id=False,
            require_name=False,
        )
        data = dict(source_data)
        data["values"] = logs
        event = {
            "type": cls._required_str(response, "type"),
            "eid": cls._required_str(response, "eid"),
            "ts": cls._required_int(response, "ts"),
            "r": result,
            "result": result,
            "data": data,
            "robot_id": robot_id,
            "values": logs,
            "logs": logs,
            "log_count": len(logs),
        }
        if portfolio is not None:
            event["portfolio"] = portfolio
        if max_time is not None:
            event["max_time"] = max_time
        return event

    @classmethod
    def _parse_deal_event(
        cls,
        response: dict[str, Any],
        *,
        subscription_id: str,
        expected_robot_id: str,
        expected_portfolio: str,
        allowed_results: set[str],
        require_max_time: bool,
    ) -> dict[str, Any]:
        cls._validate_response_identity(
            response,
            expected_type="portfolio_deals.subscribe",
            expected_eid=subscription_id,
        )
        result = cls._required_str(response, "r")
        if result not in allowed_results:
            raise VikingProtocolError(f"portfolio_deals.subscribe returned unexpected r={result!r}")
        source_data = cls._required_dict(response, "data")
        robot_id = cls._required_str(source_data, "r_id")
        portfolio = cls._required_str(source_data, "p_id")
        if robot_id != expected_robot_id or portfolio != expected_portfolio:
            raise VikingProtocolError("Unexpected portfolio-deals subscription identity")
        deals = cls._parse_deal_rows(
            source_data.get("values"),
            expected_robot_id=expected_robot_id,
            expected_portfolio=expected_portfolio,
            expected_security_key=None,
        )
        max_time = None
        if require_max_time or "mt" in source_data:
            max_time = cls._required_epoch_nsec(source_data, "mt")
        event = {
            "type": cls._required_str(response, "type"),
            "eid": cls._required_str(response, "eid"),
            "ts": cls._required_int(response, "ts"),
            "r": result,
            "result": result,
            "data": {**source_data, "values": deals},
            "robot_id": robot_id,
            "portfolio": portfolio,
            "deal_count": len(deals),
            "deals": deals,
        }
        if max_time is not None:
            event["max_time"] = max_time
        return event

    @classmethod
    def _parse_deal_rows(
        cls,
        value: Any,
        *,
        expected_robot_id: str,
        expected_portfolio: str,
        expected_security_key: str | None,
    ) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            raise VikingProtocolError("Deal response field 'values' must be an array")
        deals: list[dict[str, Any]] = []
        for index, item in enumerate(value):
            if not isinstance(item, dict):
                raise VikingProtocolError(f"Deal values[{index}] must be an object")
            for field in ("id", "cn", "sec"):
                cls._required_str(item, field)
            ono = item.get("ono")
            if isinstance(ono, bool) or not isinstance(ono, (str, int)):
                raise VikingProtocolError(f"Deal values[{index}].ono must be a string or integer")
            cls._required_epoch_nsec(item, "dt")
            for field in (
                "price",
                "orig_price",
                "buy_sell",
                "quantity",
                "decimals",
                "curpos",
                "lot_size",
            ):
                value_ = item.get(field)
                if isinstance(value_, bool) or not isinstance(value_, (int, float)):
                    raise VikingProtocolError(f"Deal values[{index}].{field} must be a number")
            row_robot_id = item.get("r_id")
            if row_robot_id is not None and row_robot_id != expected_robot_id:
                raise VikingProtocolError(f"Unexpected deal values[{index}].r_id {row_robot_id!r}")
            name = item.get("name")
            if name is not None and name != expected_portfolio:
                raise VikingProtocolError(f"Unexpected deal values[{index}].name {name!r}")
            if expected_security_key is not None and item["sec"] != expected_security_key:
                raise VikingProtocolError(f"Unexpected deal values[{index}].sec {item['sec']!r}")
            deals.append(dict(item))
        return deals

    @classmethod
    def _parse_log_rows(
        cls,
        value: Any,
        *,
        expected_robot_id: str,
        expected_portfolio: str | None,
        require_robot_time: bool,
        require_row_robot_id: bool,
        require_name: bool,
    ) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            raise VikingProtocolError("Log response field 'values' must be an array")

        logs = []
        for index, item in enumerate(value):
            if not isinstance(item, dict):
                raise VikingProtocolError(f"Log values[{index}] must be an object")
            cls._required_int(item, "level")
            cls._required_str(item, "msg")
            cls._required_epoch_nsec(item, "dt")
            if require_robot_time:
                cls._required_epoch_nsec(item, "t")

            log_id = item.get("id")
            if log_id is not None and not isinstance(log_id, str):
                raise VikingProtocolError(f"Log values[{index}].id must be a string")
            owner = item.get("owner")
            if owner is not None and not isinstance(owner, str):
                raise VikingProtocolError(f"Log values[{index}].owner must be a string or null")

            row_robot_id = item.get("r_id")
            if require_row_robot_id and not isinstance(row_robot_id, str):
                raise VikingProtocolError(f"Log values[{index}].r_id must be a string")
            if row_robot_id is not None:
                if not isinstance(row_robot_id, str):
                    raise VikingProtocolError(f"Log values[{index}].r_id must be a string")
                if row_robot_id != expected_robot_id:
                    raise VikingProtocolError(
                        f"Unexpected log values[{index}].r_id {row_robot_id!r}; "
                        f"expected {expected_robot_id!r}"
                    )

            name = item.get("name")
            if require_name and not isinstance(name, str):
                raise VikingProtocolError(f"Log values[{index}].name must be a string")
            if name is not None and not isinstance(name, str):
                raise VikingProtocolError(f"Log values[{index}].name must be a string")
            if expected_portfolio is not None and name not in {None, expected_portfolio}:
                raise VikingProtocolError(
                    f"Unexpected log values[{index}].name {name!r}; expected {expected_portfolio!r}"
                )
            logs.append(dict(item))
        return logs

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
            raise VikingProtocolError(f"Cannot parse a non-error response as an API error: r={result!r}")
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
    def _required_bool(value: dict[str, Any], key: str) -> bool:
        result = value.get(key)
        if not isinstance(result, bool):
            raise VikingProtocolError(f"{key} must be a boolean")
        return result

    @staticmethod
    def _required_epoch_nsec(value: dict[str, Any], key: str) -> int | str:
        result = value.get(key)
        if isinstance(result, int) and not isinstance(result, bool) and result >= 0:
            return result
        if isinstance(result, str) and result.isdigit():
            return result
        raise VikingProtocolError(
            f"Response field '{key}' must be a non-negative epoch_nsec integer or digit string"
        )

    @staticmethod
    def _validate_epoch_nsec_bound(value: str, field_name: str) -> None:
        if not isinstance(value, str) or not value.isdigit():
            raise ValueError(f"{field_name} must be an epoch_nsec digit string")

    @staticmethod
    def _validate_epoch_msec_bound(value: int, field_name: str) -> None:
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"{field_name} must be a non-negative epoch_msec integer")

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
