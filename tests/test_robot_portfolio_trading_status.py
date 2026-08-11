from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.config import Settings
from app.export_store import ExportStore
from app.service import MarketDataService
from app.viking_client import VikingClient


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
