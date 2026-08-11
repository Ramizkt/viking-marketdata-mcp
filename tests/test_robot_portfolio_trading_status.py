import asyncio
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
