import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from websockets.protocol import State

from app.config import Settings
from app.export_store import ExportStore
from app.response_v2 import compact_robot_state, same_build
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


async def test_robot_summary_keeps_robot_level_fields_and_drops_re():
    client = object.__new__(VikingClient)
    client._subscriptions = {"sub": object()}
    client._subscribe = AsyncMock(
        return_value={
            "type": "robot.subscribe",
            "eid": "sub",
            "ts": 1,
            "r": "s",
            "data": {
                "r_id": "1381",
                "value": {
                    "p_a": 1,
                    "p_d": 0,
                    "p_e": 0,
                    "tr": 2,
                    "rc": True,
                    "rv": "16e5431a",
                    "rvd": 1788318000,
                    "sv": "16e5431",
                    "svd": 1788318000,
                    "ps": 2,
                    "mc": 42,
                    "mdc": 2,
                    "trc": 2,
                    "dt": 1788318404142,
                    "tz": 10800,
                    "bld": "vikingrobot.vrb",
                    "future_field": "kept",
                    "re": [{"n": "alpha", "f": True, "re": True}],
                },
            },
        }
    )
    client._unsubscribe_log_subscription = AsyncMock(return_value={"unsubscribed": True})
    client.close = AsyncMock()

    result = await client.get_robot_portfolio_summary(robot_id="1381")

    state = result["robot_state"]
    assert "re" not in state
    assert state["rv"] == "16e5431a"
    assert state["sv"] == "16e5431"
    assert state["mc"] == 42
    assert state["future_field"] == "kept"
    assert result["portfolio_statuses"][0]["portfolio"] == "alpha"


def test_same_build_compares_by_prefix_not_equality():
    # api.md example pairs rv "ec1d046c" with sv "ec1d046" for one build.
    assert same_build("ec1d046c", "ec1d046") is True
    assert same_build("ec1d046", "ec1d046c") is True
    assert same_build("16e5431", "7c5a29c") is False
    assert same_build("", "7c5a29c") is None
    assert same_build(None, "7c5a29c") is None


def test_compact_robot_state_decodes_statuses_and_times():
    state = compact_robot_state(
        {
            "rc": True,
            "ps": 2,
            "tr": 0,
            "mdc": 1,
            "trc": 4,
            "rv": "16e5431a",
            "sv": "16e5431",
            "rvd": 1788318000,
            "svd": -1,
            "mc": 42,
            "dt": 1788318404142,
            "tz": 10800,
        },
        "Europe/Moscow",
    )

    assert state["connected"] is True
    assert state["process"] == "running"
    assert state["trading"] == "not_trading"
    assert state["market_data_status"] == "connecting"
    assert state["transaction_status"] == "closed_by_time"
    assert state["same_build"] is True
    assert state["server_build_differs"] is False
    assert state["main_loop_counter"] == 42
    assert state["dt_iso"] == "2026-09-02T06:06:44.142+03:00"
    assert state["rvd_iso"].startswith("2026-09-02T")
    assert state["svd_iso"] is None
    assert state["rv"] == "16e5431a"


def test_compact_robot_state_flags_differing_server_build():
    state = compact_robot_state({"rv": "7c5a29c", "sv": "16e5431"}, "Europe/Moscow")
    assert state["same_build"] is False
    assert state["server_build_differs"] is True


def test_compact_robot_state_without_versions_says_unknown():
    state = compact_robot_state({"rc": False}, "Europe/Moscow")
    assert state["same_build"] is None
    assert state["server_build_differs"] is None
    assert "dt_iso" not in state


async def test_service_get_robot_status_returns_one_compact_item(tmp_path):
    client = SimpleNamespace(
        get_robot_portfolio_summary=AsyncMock(
            return_value={
                "robot_state": {
                    "rc": True,
                    "ps": 2,
                    "rv": "16e5431a",
                    "sv": "16e5431",
                    "mc": 42,
                },
                "portfolio_statuses": [],
            }
        )
    )

    result = await _service(tmp_path, client).get_robot_status(robot_id="1381")

    assert result["row_count"] == 1
    assert result["robot_id"] == "1381"
    assert result["same_build"] is True
    assert result["detail_source"] == "robot.subscribe.value"
    assert result["items"][0]["process"] == "running"
    assert any("common prefix" in note for note in result["notes"])


async def test_service_get_robot_status_raw_keeps_payload_untouched(tmp_path):
    client = SimpleNamespace(
        get_robot_portfolio_summary=AsyncMock(
            return_value={
                "robot_state": {"rc": True, "ps": 2, "rv": "16e5431a", "sv": "16e5431"},
                "portfolio_statuses": [],
            }
        )
    )

    result = await _service(tmp_path, client).get_robot_status(robot_id="1381", raw=True)

    assert result["items"][0] == {"rc": True, "ps": 2, "rv": "16e5431a", "sv": "16e5431"}
    assert "process" not in result["items"][0]
