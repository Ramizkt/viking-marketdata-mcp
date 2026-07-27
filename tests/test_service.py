from datetime import UTC, datetime
from unittest.mock import AsyncMock

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

    async def get_portfolio_template(self, **kwargs):
        return {
            "robot_id": kwargs["robot_id"],
            "portfolio": kwargs["portfolio"],
            "template_id": "portfolio_viking_base",
            "template": {
                "template_id": "portfolio_viking_base",
                "template_fields": {
                    "portfolio": [{"field": "uf0"}],
                    "security": [{"field": "pos"}],
                },
            },
            "template_fields": {
                "portfolio": [{"field": "uf0"}],
                "security": [{"field": "pos"}],
            },
        }

    async def get_current_portfolio_data(self, **kwargs):
        return {
            "robot_id": kwargs["robot_id"],
            "portfolio": kwargs["portfolio"],
            "value": {
                "name": kwargs["portfolio"],
                "custom_field": 42,
                "securities": {},
            },
            "unsubscribed": True,
        }

    async def get_portfolio_history(self, **kwargs):
        data = {
            "buy": [{"dt": 1000, "v": 10}, {"dt": 3000, "v": 12}],
            "sell": [{"dt": 2000, "v": 11}, {"dt": 4000, "v": 13}],
        }
        return data[kwargs["key"]]

    async def get_robot_log_history(self, **kwargs):
        return {
            "robot_id": kwargs["robot_id"],
            "mint": kwargs["mint_ns"],
            "maxt": kwargs["maxt_ns"],
            "message_filter": kwargs["message_filter"],
            "limit": kwargs["limit"],
            "log_count": 1,
            "logs": [{"msg": "test"}],
        }


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


async def test_portfolio_template_preserves_all_field_groups(service):
    result = await service.get_portfolio_template(
        robot_id="1",
        portfolio="A",
    )
    assert result["template_id"] == "portfolio_viking_base"
    assert result["template_fields"]["portfolio"][0]["field"] == "uf0"
    assert result["template_fields"]["security"][0]["field"] == "pos"


async def test_current_portfolio_data_preserves_dynamic_fields(service):
    result = await service.get_current_portfolio_data(
        robot_id="1",
        portfolio="A",
    )
    assert result["value"]["custom_field"] == 42
    assert result["unsubscribed"] is True


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


def test_epoch_nsec_conversion_is_exact_and_requires_timezone():
    value = datetime(2026, 1, 1, 0, 0, 0, 123456, tzinfo=UTC)
    assert MarketDataService._to_epoch_ns(value, "date_from") == "1767225600123456000"

    with pytest.raises(ValueError, match="timezone"):
        MarketDataService._to_epoch_ns(datetime(2026, 1, 1), "date_from")


async def test_robot_log_history_converts_dates_to_epoch_nsec(service):
    result = await service.get_robot_log_history(
        robot_id="1",
        date_from=datetime(2026, 1, 1, tzinfo=UTC),
        date_to=datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),
        message_filter="*test*",
        limit=100,
    )

    assert result["mint"] == "1767225600000000000"
    assert result["maxt"] == "1767225601000000000"
    assert result["message_filter"] == "*test*"
    assert result["date_from"] == "2026-01-01T00:00:00+00:00"
    assert result["date_to"] == "2026-01-01T00:00:01+00:00"


async def test_robot_log_history_rejects_reversed_dates(service):
    with pytest.raises(ValueError, match="earlier"):
        await service.get_robot_log_history(
            robot_id="1",
            date_from=datetime(2026, 1, 2, tzinfo=UTC),
            date_to=datetime(2026, 1, 1, tzinfo=UTC),
            message_filter=None,
            limit=100,
        )


@pytest.mark.parametrize(
    ("service_method", "client_method", "kwargs"),
    [
        (
            "subscribe_portfolio_logs",
            "subscribe_portfolio_logs",
            {"robot_id": "1", "portfolio": "A"},
        ),
        (
            "unsubscribe_portfolio_logs",
            "unsubscribe_portfolio_logs",
            {"subscription_id": "portfolio-logs-sub-1"},
        ),
        (
            "subscribe_robot_logs",
            "subscribe_robot_logs",
            {"robot_id": "1"},
        ),
        (
            "unsubscribe_robot_logs",
            "unsubscribe_robot_logs",
            {"subscription_id": "robot-logs-sub-1"},
        ),
    ],
)
async def test_log_service_methods_delegate(service, service_method, client_method, kwargs):
    mock = AsyncMock(return_value={"ok": True})
    setattr(service.client, client_method, mock)

    result = await getattr(service, service_method)(**kwargs)

    assert result == {"ok": True}
    if "subscription_id" in kwargs:
        mock.assert_awaited_once_with(kwargs["subscription_id"])
    else:
        mock.assert_awaited_once_with(**kwargs)


@pytest.mark.parametrize(
    ("service_method", "client_method"),
    [
        ("get_portfolio_log_updates", "get_portfolio_log_updates"),
        ("get_robot_log_updates", "get_robot_log_updates"),
    ],
)
async def test_log_update_service_methods_delegate(service, service_method, client_method):
    mock = AsyncMock(return_value={"event_count": 0})
    setattr(service.client, client_method, mock)

    result = await getattr(service, service_method)(
        subscription_id="logs-sub-1",
        wait_seconds=3,
        max_events=25,
    )

    assert result == {"event_count": 0}
    mock.assert_awaited_once_with(
        "logs-sub-1",
        wait_seconds=3,
        max_events=25,
    )
