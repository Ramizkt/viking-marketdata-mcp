from __future__ import annotations

import asyncio
import json
import logging
import uuid
import zlib
from collections.abc import Iterable
from typing import Any

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed
from websockets.protocol import State

from app.config import Settings
from app.credentials import VikingCredentials

logger = logging.getLogger(__name__)


class VikingAPIError(RuntimeError):
    """Error returned by the Viking WebSocket API."""

    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


class VikingClientPool:
    """Reuse one persistent Viking WebSocket per set of user credentials."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._clients: dict[str, VikingClient] = {}

    def get(self, credentials: VikingCredentials) -> VikingClient:
        fingerprint = credentials.fingerprint
        client = self._clients.get(fingerprint)
        if client is None:
            client = VikingClient(self.settings, credentials)
            self._clients[fingerprint] = client
        return client

    async def close(self) -> None:
        clients = list(self._clients.values())
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
        self._reader_task: asyncio.Task[None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None

    @property
    def connected(self) -> bool:
        return self._ws is not None and self._ws.state is State.OPEN

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
        all_response = await self.request("available_portfolio_list.subscribe", {})
        all_rows = all_response.get("data", {}).get("portfolios_add", [])
        try:
            await self.request(
                "available_portfolio_list.unsubscribe",
                {"sub_eid": all_response["eid"]},
            )
        except Exception:
            logger.warning("Could not unsubscribe from available portfolio list", exc_info=True)

        history_response = await self.request("available_portfolio_list.get_with_history", {})
        history_rows = history_response.get("data", {}).get("portfolios", [])
        history_ids = {(str(row[0]), str(row[1])) for row in history_rows if len(row) >= 2}

        result = []
        for row in all_rows:
            if len(row) < 2:
                continue
            robot_id = str(row[0])
            portfolio = str(row[1])
            result.append(
                {
                    "robot_id": robot_id,
                    "portfolio": portfolio,
                    "owner": str(row[2]) if len(row) > 2 else None,
                    "history_available": (robot_id, portfolio) in history_ids,
                }
            )

        result.sort(key=lambda item: (item["robot_id"], item["portfolio"]))
        return result

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

        if response.get("r") == "e":
            error = response.get("data", {})
            raise VikingAPIError(
                str(error.get("msg", "Unknown Viking API error")),
                code=error.get("code"),
            )
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
