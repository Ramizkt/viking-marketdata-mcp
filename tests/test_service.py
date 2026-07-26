from datetime import UTC, datetime

import pytest

from app.config import Settings
from app.export_store import ExportStore
from app.service import MISSING_VALUE, MarketDataService


class FakeClient:
    async def list_portfolios(self):
        return [
            {
                "robot_id": "1",
                "portfolio": "A",
                "owner": "owner@example.com",
                "history_available": True,
            },
            {
                "robot_id": "1",
                "portfolio": "B",
                "owner": "owner@example.com",
                "history_available": False,
            },
        ]

    async def get_portfolio_history(self, **kwargs):
        data = {
            "buy": [{"dt": 1000, "v": 10}, {"dt": 3000, "v": 12}],
            "sell": [{"dt": 2000, "v": 11}, {"dt": 4000, "v": 13}],
        }
        return data[kwargs["key"]]


@pytest.fixture
def service(tmp_path):
    settings = Settings(
        inline_max_rows=2,
        inline_max_bytes=100_000,
        export_dir=tmp_path,
        public_base_url="https://example.test",
        export_signing_key="test-signing-key",
    )
    return MarketDataService(settings, FakeClient(), ExportStore(settings))


async def test_list_history_only(service):
    result = await service.list_available_portfolios(history_only=True)
    assert result["count"] == 1
    assert result["portfolios"][0]["portfolio"] == "A"


def test_merge_fields_forward_fills():
    rows = MarketDataService._merge_fields(
        {
            "buy": [{"dt": 1000, "v": 10}, {"dt": 3000, "v": 12}],
            "sell": [{"dt": 2000, "v": 11}, {"dt": 4000, "v": 13}],
        },
        ["buy", "sell"],
    )
    assert rows == [
        {"timestamp": 2000, "buy": 10, "sell": 11},
        {"timestamp": 3000, "buy": 12, "sell": 11},
        {"timestamp": 4000, "buy": 12, "sell": 13},
    ]


def test_merge_fields_resets_missing_value():
    rows = MarketDataService._merge_fields(
        {
            "buy": [
                {"dt": 1000, "v": 10},
                {"dt": 2000, "v": MISSING_VALUE},
                {"dt": 3000, "v": 12},
            ]
        },
        ["buy"],
    )
    assert rows == [
        {"timestamp": 1000, "buy": 10},
        {"timestamp": 3000, "buy": 12},
    ]


def test_delivery_inline_is_overridden(service):
    actual, reason = service._choose_delivery(
        requested="inline",
        row_count=3,
        serialized_bytes=100,
    )
    assert actual == "file"
    assert reason


async def test_file_delivery_contains_signed_url(service):
    result = await service.get_portfolio_data(
        robot_id="1",
        portfolio="A",
        date_from=datetime.fromtimestamp(0, tz=UTC),
        date_to=datetime.fromtimestamp(10, tz=UTC),
        fields=["buy", "sell"],
        aggregation="raw",
        delivery="auto",
        preview_rows=1,
    )
    assert result.structured["actual_delivery"] == "file"
    assert result.exported_file is not None
    assert result.exported_file.download_url.startswith("https://example.test/downloads/")
    assert result.exported_file.path.exists()


def test_naive_datetime_is_rejected():
    with pytest.raises(ValueError, match="timezone"):
        MarketDataService._to_epoch_ms(datetime(2026, 1, 1), "date_from")
