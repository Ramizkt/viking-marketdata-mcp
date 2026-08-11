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


async def test_robot_summary_uses_re_even_when_disabled_counter_disagrees():
    client = object.__new__(VikingClient)
    client._subscriptions = {"sub": object()}
    client._subscribe = AsyncMock(
        return_value={
            "type": "robot.subscribe",
            "eid": "sub",
            "ts": 1,
            "r": "s",
            "data": {
                "r_id": "998",
                "value": {
                    "p_a": 2,
                    "p_d": 2,
                    "p_e": 0,
                    "tr": 2,
                    "re": [
                        {"n": "alpha", "f": True, "re": True},
                        {"n": "beta", "f": True, "re": False},
                    ],
                },
            },
        }
    )
    client._unsubscribe_log_subscription = AsyncMock(return_value={"unsubscribed": True})
    client.close = AsyncMock()
    result = await client.get_robot_portfolio_summary(robot_id="998")
    assert result["disabled_portfolios"] == 2
    assert result["trading_portfolios"] == 1
    assert result["portfolio_statuses"][0]["status"] == "trading"
    assert result["portfolio_statuses"][0]["re"] is True


def _service(tmp_path, client):
    settings = Settings(
        export_dir=tmp_path, public_base_url="https://example.test", export_signing_key="test"
    )
    return MarketDataService(settings, client, ExportStore(settings))


async def test_service_returns_all_re_statuses_without_fanout(tmp_path):
    client = SimpleNamespace(
        get_robot_portfolio_summary=AsyncMock(
            return_value={
                "all_portfolios": 2,
                "disabled_portfolios": 2,
                "expired_portfolios": 0,
                "robot_trading_status": 2,
                "robot_trading": True,
                "portfolio_status_count": 2,
                "trading_portfolios": 1,
                "not_trading_portfolios": 1,
                "portfolio_statuses": [
                    {"portfolio": "alpha", "trading": True, "status": "trading", "re": True, "f": True},
                    {"portfolio": "beta", "trading": False, "status": "not_trading", "re": False, "f": True},
                ],
            }
        )
    )
    result = await _service(tmp_path, client).get_robot_portfolio_trading_status(robot_id="998")
    assert result["detail_source"] == "robot.subscribe.value.re"
    assert result["per_portfolio_reads"] == 0
    assert [x["status"] for x in result["items"]] == ["trading", "not_trading"]


async def test_trading_only_filters_re_true(tmp_path):
    client = SimpleNamespace(
        get_robot_portfolio_summary=AsyncMock(
            return_value={
                "all_portfolios": 2,
                "disabled_portfolios": 0,
                "expired_portfolios": 0,
                "robot_trading_status": 2,
                "robot_trading": True,
                "portfolio_status_count": 2,
                "trading_portfolios": 1,
                "not_trading_portfolios": 1,
                "portfolio_statuses": [
                    {"portfolio": "alpha", "trading": True, "status": "trading", "re": True, "f": True},
                    {"portfolio": "beta", "trading": False, "status": "not_trading", "re": False, "f": True},
                ],
            }
        )
    )
    result = await _service(tmp_path, client).get_robot_portfolio_trading_status(
        robot_id="998", trading_only=True
    )
    assert [x["portfolio"] for x in result["items"]] == ["alpha"]
