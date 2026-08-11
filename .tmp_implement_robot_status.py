from pathlib import Path

def replace_once(path: str, old: str, new: str) -> None:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if text.count(old) != 1:
        raise SystemExit(f"{path}: expected anchor exactly once")
    p.write_text(text.replace(old, new, 1), encoding="utf-8")

client_path = Path("app/viking_client.py")
client = client_path.read_text(encoding="utf-8")

list_anchor = '''    async def list_portfolios(self) -> list[dict[str, Any]]:
        """Return all accessible portfolios and whether history is enabled."""
        snapshot = await self.subscribe_available_portfolios()
        all_rows = snapshot["portfolios_add"]
        try:
  await self.unsubscribe_available_portfolios(snapshot["subscription_id"])
        except Exception:
  logger.warning("Could not unsubscribe from available portfolio list", exc_info=True)

        history_response = await self.request("available_portfolio_list.get_with_history", {})
'''
list_replacement = '''    async def list_available_portfolios_basic(self) -> list[dict[str, Any]]:
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
'''
if client.count(list_anchor) != 1:
    raise SystemExit("app/viking_client.py: list_portfolios anchor mismatch")
client = client.replace(list_anchor, list_replacement, 1)

portfolio_anchor = '''    async def subscribe_portfolio(
        self,
        *,
        robot_id: str,
        portfolio: str,
    ) -> dict[str, Any]:
'''
aggregate_methods = '''    async def get_robot_portfolio_summary(self, *, robot_id: str) -> dict[str, Any]:
        """Return robot-wide portfolio counters from one robot.subscribe snapshot."""
        if not robot_id:
  raise ValueError("robot_id must not be empty")
        response = await self._subscribe("robot.subscribe", {"r_id": robot_id})
        subscription_id = self._required_str(response, "eid")
        try:
  self._validate_response_identity(
      response,
      expected_type="robot.subscribe",
      expected_eid=subscription_id,
  )
  result = self._required_str(response, "r")
  if result != "s":
      raise VikingProtocolError(
          "robot.subscribe returned an unexpected result; expected r='s'"
      )
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

        return {
  "robot_id": robot_id,
  "all_portfolios": all_portfolios,
  "disabled_portfolios": disabled_portfolios,
  "enabled_portfolios": all_portfolios - disabled_portfolios,
  "expired_portfolios": expired_portfolios,
  "robot_trading_status": trading_status,
  "robot_trading": (
      False if trading_status == 0 else True if trading_status == 2 else None
  ),
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
  ("portfolio.subscribe", {"r_id": robot_id, "p_id": portfolio})
  for portfolio in portfolios
        ]
        grouped = await self._grouped_exchange(
  subscribe_requests,
  register_subscriptions=True,
        )

        items: list[dict[str, Any]] = []
        active_subscriptions: list[tuple[str, str]] = []
        try:
  for portfolio, (subscription_id, outcome) in zip(
      portfolios, grouped, strict=True
  ):
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

'''
if client.count(portfolio_anchor) != 1:
    raise SystemExit("app/viking_client.py: subscribe_portfolio anchor mismatch")
client = client.replace(portfolio_anchor, aggregate_methods + portfolio_anchor, 1)

transport_anchor = '''    async def _ensure_connected(self) -> None:
'''
grouped_helper = '''    async def _grouped_exchange(
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
  for (eid, message_type, _, _, _), response in zip(
      records, responses, strict=True
  ):
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

'''
if client.count(transport_anchor) != 1:
    raise SystemExit("app/viking_client.py: _ensure_connected anchor mismatch")
client = client.replace(transport_anchor, grouped_helper + transport_anchor, 1)
client_path.write_text(client, encoding="utf-8")

service_path = Path("app/service.py")
service = service_path.read_text(encoding="utf-8")
service_anchor = '''    async def subscribe_available_portfolios(self) -> dict[str, Any]:
'''
service_method = '''    async def get_robot_portfolio_trading_status(
        self,
        *,
        robot_id: str,
        include_items: bool = False,
        enabled_only: bool = False,
    ) -> dict[str, Any]:
        if not robot_id:
  raise ValueError("robot_id must not be empty")
        if enabled_only and not include_items:
  raise ValueError("enabled_only=true requires include_items=true")

        robot_summary = await self.client.get_robot_portfolio_summary(robot_id=robot_id)
        available_rows = await self.client.list_available_portfolios_basic()
        robot_rows = [row for row in available_rows if row["robot_id"] == robot_id]
        accessible_total = len(robot_rows)
        robot_total = robot_summary["all_portfolios"]
        notes = [
  "trading_enabled is defined strictly as portfolio snapshot field disabled == false; "
  "it does not by itself prove that the robot, transaction connection or market-data "
  "connection is currently trading-ready."
        ]

        scan_required = include_items or accessible_total != robot_total
        all_items: list[dict[str, Any]] = []
        if not scan_required:
  accessible_enabled = robot_summary["enabled_portfolios"]
  accessible_disabled = robot_summary["disabled_portfolios"]
  accessible_unknown = 0
  detail_source = "robot.subscribe.p_a/p_d"
  notes.append(
      "The current role can access every portfolio counted by robot.subscribe, so "
      "accessible counts were derived from p_a/p_d without per-portfolio reads."
  )
  cleanup_reconnected = False
        else:
  batch = await self.client.get_current_portfolio_data_many(
      robot_id=robot_id,
      portfolios=[row["portfolio"] for row in robot_rows],
  )
  by_name = {item["portfolio"]: item for item in batch["items"]}
  accessible_enabled = 0
  accessible_disabled = 0
  accessible_unknown = 0
  for row in robot_rows:
      portfolio = row["portfolio"]
      batch_item = by_name.get(portfolio)
      item: dict[str, Any] = {
          "robot_id": robot_id,
          "portfolio": portfolio,
          "owner": row["owner"],
      }
      if batch_item is None or not batch_item.get("ok"):
          accessible_unknown += 1
          item.update(
              {
                  "status": "unknown",
                  "trading_enabled": None,
                  "disabled": None,
                  "source": "portfolio.subscribe.value.disabled",
              }
          )
          if batch_item is not None:
              item["error_type"] = batch_item.get("error_type")
              item["message"] = batch_item.get("message")
              if "code" in batch_item:
                  item["code"] = batch_item["code"]
      else:
          disabled = batch_item["value"].get("disabled")
          if isinstance(disabled, bool):
              trading_enabled = not disabled
              if trading_enabled:
                  accessible_enabled += 1
                  status = "enabled"
              else:
                  accessible_disabled += 1
                  status = "disabled"
              item.update(
                  {
                      "status": status,
                      "trading_enabled": trading_enabled,
                      "disabled": disabled,
                      "source": "portfolio.subscribe.value.disabled",
                  }
              )
          else:
              accessible_unknown += 1
              item.update(
                  {
                      "status": "unknown",
                      "trading_enabled": None,
                      "disabled": None,
                      "source": "portfolio.subscribe.value.disabled",
                      "reason": "disabled field is missing or is not boolean",
                  }
              )
      all_items.append(item)
  detail_source = "batched portfolio.subscribe"
  cleanup_reconnected = batch["cleanup_reconnected"]
  if accessible_total != robot_total:
      notes.append(
          "robot.subscribe p_a/p_d are robot-wide counters. Because the current role "
          "does not expose the same number of portfolios, accessible counts were "
          "computed only from accessible portfolio snapshots."
      )
  if cleanup_reconnected:
      notes.append(
          "At least one grouped unsubscribe did not complete cleanly; the Viking "
          "WebSocket was closed to guarantee subscription cleanup and will reconnect "
          "on the next request."
      )

        items: list[dict[str, Any]] = []
        if include_items:
  items = all_items
  if enabled_only:
      items = [item for item in items if item["trading_enabled"] is True]

        return envelope(
  items,
  data_status="partially_available" if accessible_unknown else "ok",
  notes=notes,
  robot_id=robot_id,
  robot_total_count=robot_summary["all_portfolios"],
  robot_enabled_count=robot_summary["enabled_portfolios"],
  robot_disabled_count=robot_summary["disabled_portfolios"],
  robot_expired_count=robot_summary["expired_portfolios"],
  robot_trading_status=robot_summary["robot_trading_status"],
  robot_trading=robot_summary["robot_trading"],
  accessible_total_count=accessible_total,
  accessible_enabled_count=accessible_enabled,
  accessible_disabled_count=accessible_disabled,
  accessible_unknown_count=accessible_unknown,
  include_items=include_items,
  enabled_only=enabled_only,
  returned_count=len(items),
  detail_source=detail_source,
  grouped_request_max_size=50 if scan_required else None,
  cleanup_reconnected=cleanup_reconnected,
        )

'''
if service.count(service_anchor) != 1:
    raise SystemExit("app/service.py: subscribe_available anchor mismatch")
service = service.replace(service_anchor, service_method + service_anchor, 1)
service_path.write_text(service, encoding="utf-8")

main_path = Path("app/main.py")
main = main_path.read_text(encoding="utf-8")
instruction_old = (
    '        "Текущее полное состояние портфеля получай через get_current_portfolio_data. "\n'
)
instruction_new = instruction_old + (
    '        "Для вопросов о количестве включённых/выключенных портфелей робота или о том, "\n'
    '        "какие именно портфели включены, всегда используй get_robot_portfolio_trading_status; "\n'
    '        "не делай fan-out из get_current_portfolio_data по каждому портфелю. "\n'
)
if main.count(instruction_old) != 1:
    raise SystemExit("app/main.py: instructions anchor mismatch")
main = main.replace(instruction_old, instruction_new, 1)

tool_anchor = '''@mcp.tool(
    title="Подписаться на доступные портфели",
'''
tool_block = '''@mcp.tool(
    title="Статус торговли портфелей робота",
    description=(
        "Одним read-only MCP-вызовом возвращает robot-wide счётчики p_a/p_d/p_e и точные "
        "счётчики по портфелям, доступным текущей роли. trading_enabled означает строго "
        "disabled=false. Для быстрого ответа только по количествам оставь include_items=false. "
        "Чтобы получить конкретные портфели, установи include_items=true; enabled_only=true "
        "вернёт только явно включённые. Массовые portfolio.subscribe выполняются внутри сервера "
        "группами до 50 сообщений, поэтому агент не должен вызывать get_current_portfolio_data "
        "по каждому портфелю отдельно."
    ),
    annotations=READ_ONLY,
)
async def get_robot_portfolio_trading_status(
    robot_id: Annotated[str, Field(min_length=1, description="Идентификатор робота")],
    include_items: bool = False,
    enabled_only: bool = False,
) -> CallToolResult:
    try:
        result = await _service_for_request().get_robot_portfolio_trading_status(
  robot_id=robot_id,
  include_items=include_items,
  enabled_only=enabled_only,
        )
    except SUBSCRIPTION_ERRORS as exc:
        logger.warning("Robot portfolio trading status request failed: %s", exc)
        return _error_result(exc)
    return CallToolResult(
        content=[
  TextContent(
      type="text",
      text=(
          f"Робот {robot_id}: доступно текущей роли "
          f"{result['accessible_total_count']} портфелей; "
          f"явно включено {result['accessible_enabled_count']}, "
          f"выключено {result['accessible_disabled_count']}, "
          f"неизвестно {result['accessible_unknown_count']}."
      ),
  )
        ],
        structuredContent=result,
    )


'''
if main.count(tool_anchor) != 1:
    raise SystemExit("app/main.py: tool insertion anchor mismatch")
main = main.replace(tool_anchor, tool_block + tool_anchor, 1)
main_path.write_text(main, encoding="utf-8")

tests = r'''import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from websockets.protocol import State

from app.config import Settings
from app.export_store import ExportStore
from app.service import MarketDataService
from app.viking_client import VikingClient


class _GroupedFakeWebSocket:
    state = State.OPEN

    def __init__(self, client):
        self.client = client
        self.sent = []

    async def send(self, raw):
        payloads = json.loads(raw)
        assert isinstance(payloads, list)
        assert 1 <= len(payloads) <= 50
        self.sent.append(payloads)
        for payload in payloads:
  eid = payload["eid"]
  message_type = payload["type"]
  if message_type == "portfolio.subscribe":
      portfolio = payload["data"]["p_id"]
      response = {
          "type": message_type,
          "eid": eid,
          "ts": 1,
          "r": "s",
          "data": {
              "r_id": payload["data"]["r_id"],
              "p_id": portfolio,
              "value": {
                  "name": portfolio,
                  "disabled": portfolio.endswith("-disabled"),
                  "securities": {},
              },
          },
      }
  else:
      response = {
          "type": message_type,
          "eid": eid,
          "ts": 2,
          "r": "p",
          "data": {},
      }
  self.client._pending[eid].set_result(response)


async def test_grouped_current_portfolio_reads_use_max_50_messages():
    client = object.__new__(VikingClient)
    client.settings = SimpleNamespace(viking_request_timeout_seconds=1)
    client._pending = {}
    client._subscriptions = {}
    client._send_lock = asyncio.Lock()
    client._ensure_connected = AsyncMock()
    client.close = AsyncMock()
    ws = _GroupedFakeWebSocket(client)
    client._ws = ws

    portfolios = [f"p-{index}" for index in range(51)]
    result = await client.get_current_portfolio_data_many(
        robot_id="998",
        portfolios=portfolios,
    )

    assert result["item_count"] == 51
    assert result["cleanup_reconnected"] is False
    assert [len(group) for group in ws.sent] == [50, 1, 50, 1]
    assert all(item["ok"] for item in result["items"])
    assert client._subscriptions == {}


async def test_robot_portfolio_summary_uses_documented_counters():
    client = object.__new__(VikingClient)
    client._subscriptions = {"robot-sub": object()}
    client._subscribe = AsyncMock(
        return_value={
  "type": "robot.subscribe",
  "eid": "robot-sub",
  "ts": 1,
  "r": "s",
  "data": {
      "r_id": "998",
      "value": {"p_a": 126, "p_d": 20, "p_e": 3, "tr": 2},
  },
        }
    )
    client._unsubscribe_log_subscription = AsyncMock(return_value={"unsubscribed": True})
    client.close = AsyncMock()

    result = await client.get_robot_portfolio_summary(robot_id="998")

    assert result["all_portfolios"] == 126
    assert result["disabled_portfolios"] == 20
    assert result["enabled_portfolios"] == 106
    assert result["expired_portfolios"] == 3
    assert result["robot_trading"] is True
    client._unsubscribe_log_subscription.assert_awaited_once_with(
        "robot-sub",
        expected_subscribe_type="robot.subscribe",
        unsubscribe_type="robot.unsubscribe",
    )


def _service(tmp_path, client):
    settings = Settings(
        export_dir=tmp_path,
        public_base_url="https://example.test",
        export_signing_key="test-signing-key",
    )
    return MarketDataService(settings, client, ExportStore(settings))


async def test_status_summary_avoids_portfolio_fanout_when_all_are_accessible(tmp_path):
    client = SimpleNamespace(
        get_robot_portfolio_summary=AsyncMock(
  return_value={
      "all_portfolios": 2,
      "disabled_portfolios": 1,
      "enabled_portfolios": 1,
      "expired_portfolios": 0,
      "robot_trading_status": 2,
      "robot_trading": True,
  }
        ),
        list_available_portfolios_basic=AsyncMock(
  return_value=[
      {"robot_id": "998", "portfolio": "A", "owner": "one@example.com"},
      {"robot_id": "998", "portfolio": "B", "owner": "two@example.com"},
  ]
        ),
        get_current_portfolio_data_many=AsyncMock(),
    )

    result = await _service(tmp_path, client).get_robot_portfolio_trading_status(
        robot_id="998"
    )

    assert result["accessible_total_count"] == 2
    assert result["accessible_enabled_count"] == 1
    assert result["accessible_disabled_count"] == 1
    assert result["accessible_unknown_count"] == 0
    assert result["detail_source"] == "robot.subscribe.p_a/p_d"
    client.get_current_portfolio_data_many.assert_not_awaited()


async def test_status_details_batch_and_filter_enabled_portfolios(tmp_path):
    client = SimpleNamespace(
        get_robot_portfolio_summary=AsyncMock(
  return_value={
      "all_portfolios": 3,
      "disabled_portfolios": 1,
      "enabled_portfolios": 2,
      "expired_portfolios": 0,
      "robot_trading_status": 2,
      "robot_trading": True,
  }
        ),
        list_available_portfolios_basic=AsyncMock(
  return_value=[
      {"robot_id": "998", "portfolio": "A", "owner": "one@example.com"},
      {"robot_id": "998", "portfolio": "B", "owner": "two@example.com"},
      {"robot_id": "998", "portfolio": "C", "owner": "three@example.com"},
  ]
        ),
        get_current_portfolio_data_many=AsyncMock(
  return_value={
      "items": [
          {"portfolio": "A", "ok": True, "value": {"disabled": False}},
          {"portfolio": "B", "ok": True, "value": {"disabled": True}},
          {"portfolio": "C", "ok": True, "value": {}},
      ],
      "cleanup_reconnected": False,
  }
        ),
    )

    result = await _service(tmp_path, client).get_robot_portfolio_trading_status(
        robot_id="998",
        include_items=True,
        enabled_only=True,
    )

    assert result["data_status"] == "partially_available"
    assert result["accessible_enabled_count"] == 1
    assert result["accessible_disabled_count"] == 1
    assert result["accessible_unknown_count"] == 1
    assert [item["portfolio"] for item in result["items"]] == ["A"]
    client.get_current_portfolio_data_many.assert_awaited_once_with(
        robot_id="998",
        portfolios=["A", "B", "C"],
    )


async def test_partial_access_scans_even_for_count_only(tmp_path):
    client = SimpleNamespace(
        get_robot_portfolio_summary=AsyncMock(
  return_value={
      "all_portfolios": 3,
      "disabled_portfolios": 1,
      "enabled_portfolios": 2,
      "expired_portfolios": 0,
      "robot_trading_status": 2,
      "robot_trading": True,
  }
        ),
        list_available_portfolios_basic=AsyncMock(
  return_value=[
      {"robot_id": "998", "portfolio": "A", "owner": "one@example.com"},
      {"robot_id": "998", "portfolio": "B", "owner": "two@example.com"},
  ]
        ),
        get_current_portfolio_data_many=AsyncMock(
  return_value={
      "items": [
          {"portfolio": "A", "ok": True, "value": {"disabled": False}},
          {"portfolio": "B", "ok": True, "value": {"disabled": True}},
      ],
      "cleanup_reconnected": False,
  }
        ),
    )

    result = await _service(tmp_path, client).get_robot_portfolio_trading_status(
        robot_id="998"
    )

    assert result["items"] == []
    assert result["accessible_total_count"] == 2
    assert result["accessible_enabled_count"] == 1
    assert result["accessible_disabled_count"] == 1
    assert result["robot_total_count"] == 3
    assert result["detail_source"] == "batched portfolio.subscribe"
'''
Path("tests/test_robot_portfolio_trading_status.py").write_text(tests, encoding="utf-8")

readme = Path("README.md")
readme_text = readme.read_text(encoding="utf-8")
marker = "## Robot portfolio trading status"
if marker not in readme_text:
    readme_text += '''\n\n## Robot portfolio trading status\n\nFor questions such as “how many portfolios have trading enabled?” or “which portfolios are enabled?”, use the single read-only MCP tool `get_robot_portfolio_trading_status`. `trading_enabled` is defined strictly as the current portfolio field `disabled == false`.\n\n- `include_items=false` returns counts only. When the current role can access every portfolio counted by `robot.subscribe`, the server derives the counts directly from documented `p_a` (all portfolios) and `p_d` (disabled portfolios), without reading every portfolio.\n- `include_items=true` returns compact per-portfolio statuses. `enabled_only=true` keeps only explicitly enabled portfolios.\n- If access covers only part of a robot, accessible counts are computed from accessible portfolio snapshots; robot-wide `p_a/p_d/p_e` are still returned separately.\n- Mass snapshot reads are performed inside Railway with documented Viking JSON request groups of at most 50 messages, so MCP clients should not fan out `get_current_portfolio_data` once per portfolio.\n- Missing/non-boolean `disabled` values and per-portfolio API errors are reported as `unknown`, never silently treated as disabled.\n'''
    readme.write_text(readme_text, encoding="utf-8")

agents = Path("AGENTS.md")
agents_text = agents.read_text(encoding="utf-8")
marker = "## Robot portfolio trading aggregate"
if marker not in agents_text:
    agents_text += '''\n\n## Robot portfolio trading aggregate\n\n- `get_robot_portfolio_trading_status` is the canonical tool for enabled/disabled portfolio counts and names. Agents must not fan out `get_current_portfolio_data` for every portfolio to answer this class of question.\n- Fast count path: `robot.subscribe` -> snapshot fields `p_a` (all), `p_d` (disabled), `p_e` (expired), `tr` -> `robot.unsubscribe`. `enabled = p_a - p_d`. The API documents `p_a/p_d/p_e` as robot-wide counters; do not present them as access-scoped when the current role sees only a subset.\n- Accessible identities come from `available_portfolio_list.subscribe`/`unsubscribe` without the extra history lookup.\n- Detailed names/statuses use `portfolio.subscribe.value.disabled`; only boolean `false` means explicitly enabled and boolean `true` means disabled. Missing/non-boolean values or per-portfolio errors are `unknown`.\n- Detailed reads are sent to Viking as JSON request groups with a hard maximum of 50 messages, matching the official API grouping limit. A whole group costs 49 rate-limit points according to the API, so preserve grouped transport for large robot scans.\n- Grouped subscriptions must always be grouped-unsubscribed. If cleanup acknowledgements fail, close the upstream Viking WebSocket so all subscriptions are guaranteed to be dropped; the pooled client reconnects on the next request.\n- `trading_enabled` here is only the inverse of the portfolio `disabled` field. It does not imply the robot process or its transaction/market-data connections are currently trading-ready.\n'''
    agents.write_text(agents_text, encoding="utf-8")
