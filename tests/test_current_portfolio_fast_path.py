from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import Settings
from app.export_store import ExportStore
from app.service import MarketDataService
from app.viking_client import VikingAPIError


def _make_service(tmp_path, *, current_result=None, current_error=None, portfolios=None):
    settings = Settings(
        export_dir=tmp_path,
        public_base_url="https://example.test",
        export_signing_key="test-signing-key",
    )
    current = AsyncMock()
    if current_error is not None:
        current.side_effect = current_error
    else:
        current.return_value = current_result or {
            "robot_id": "1",
            "portfolio": "A",
            "value": {"name": "A", "disabled": False, "securities": {}},
            "unsubscribed": True,
        }
    if portfolios is None:
        portfolios = [
            {
                "robot_id": "1",
                "portfolio": "A",
                "owner": "owner@example.com",
                "history_available": True,
            }
        ]
    client = SimpleNamespace(
        get_current_portfolio_data=current,
        list_portfolios=AsyncMock(return_value=portfolios),
    )
    return MarketDataService(settings, client, ExportStore(settings)), client


async def test_current_portfolio_success_skips_portfolio_list_preflight(tmp_path):
    service, client = _make_service(tmp_path)

    result = await service.get_current_portfolio_data(robot_id="1", portfolio="A")

    assert result["items"][0]["disabled"] is False
    client.get_current_portfolio_data.assert_awaited_once_with(robot_id="1", portfolio="A")
    client.list_portfolios.assert_not_awaited()


async def test_current_portfolio_api_error_enriches_accessible_not_found(tmp_path):
    service, client = _make_service(
        tmp_path,
        current_error=VikingAPIError("Permission denied", code=555),
        portfolios=[
            {
                "robot_id": "1",
                "portfolio": "Alpha",
                "owner": "owner@example.com",
                "history_available": True,
            },
            {
                "robot_id": "1",
                "portfolio": "Beta",
                "owner": "owner@example.com",
                "history_available": False,
            },
        ],
    )

    result = await service.get_current_portfolio_data(robot_id="1", portfolio="Alphx")

    assert result["error_type"] == "portfolio_not_found"
    assert result["similar_portfolios"] == ["Alpha"]
    client.list_portfolios.assert_awaited_once_with()


async def test_current_portfolio_api_error_is_preserved_for_inaccessible_robot(tmp_path):
    original = VikingAPIError("Permission denied", code=555)
    service, client = _make_service(
        tmp_path,
        current_error=original,
        portfolios=[
            {
                "robot_id": "1",
                "portfolio": "Alpha",
                "owner": "owner@example.com",
                "history_available": True,
            }
        ],
    )

    with pytest.raises(VikingAPIError) as exc_info:
        await service.get_current_portfolio_data(robot_id="foreign", portfolio="hidden")

    assert exc_info.value is original
    client.list_portfolios.assert_awaited_once_with()
