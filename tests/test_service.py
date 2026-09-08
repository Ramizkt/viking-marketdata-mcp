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

    async def get_messages_history(self, **kwargs):
        return {
            "mint": kwargs["mint_ms"],
            "maxt": kwargs["maxt_ms"],
            "read": kwargs["read"],
            "limit": kwargs["limit"],
            "count": 3,
            "message_count": 2,
            "messages": [
                {
                    "st": 0,
                    "dt": 1788275520000,
                    "msg": "The robot 1381 will be restarted on Sep 02, 2026, at 06:05 Moscow time.",
                },
                {"st": 1, "dt": "1788166800000", "msg": "Test msg 2"},
            ],
        }

    async def get_previous_messages(self, **kwargs):
        return {
            "older_than": kwargs["older_than_ms"],
            "read": kwargs["read"],
            "limit": kwargs["limit"],
            "count": 1 if kwargs["read"] else None,
            "message_count": 1 if kwargs["read"] else 0,
            "messages": (
                [{"st": 1, "dt": 1788166800000, "msg": "Test msg 2"}] if kwargs["read"] else []
            ),
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
    assert result["returned_count"] == 1
    assert result["items"][0]["portfolio"] == "A"


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
    assert result["items"][0]["custom_field"] == 42
    assert result["subscription_closed"] is True


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


async def test_empty_portfolio_history_has_explicit_reason(service):
    service.client.get_portfolio_history = AsyncMock(return_value=[])
    result = await service.get_portfolio_data(
        robot_id="1",
        portfolio="A",
        date_from=datetime.fromtimestamp(0, tz=UTC),
        date_to=datetime.fromtimestamp(10, tz=UTC),
        fields=["buy"],
        aggregation="raw",
        delivery="inline",
        preview_rows=10,
    )
    assert result.structured["data_status"] == "no_data_in_range"
    assert result.structured["items"] == []
    assert result.structured["notes"]


async def test_unknown_robot_does_not_raise_stop_iteration(service):
    service.client.get_portfolio_history = AsyncMock(return_value=[])
    result = await service.get_portfolio_data(
        robot_id="foreign",
        portfolio="hidden",
        date_from=datetime.fromtimestamp(0, tz=UTC),
        date_to=datetime.fromtimestamp(10, tz=UTC),
        fields=["buy"],
        aggregation="raw",
        delivery="inline",
        preview_rows=10,
    )
    assert result.structured["data_status"] == "no_data_in_range"


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

    assert result["row_count"] == 1
    assert result["coverage"]["from"] == "2026-01-01T00:00:00+00:00"
    assert result["coverage"]["to"] == "2026-01-01T00:00:01+00:00"


async def test_robot_log_history_rejects_reversed_dates(service):
    with pytest.raises(ValueError, match="earlier"):
        await service.get_robot_log_history(
            robot_id="1",
            date_from=datetime(2026, 1, 2, tzinfo=UTC),
            date_to=datetime(2026, 1, 1, tzinfo=UTC),
            message_filter=None,
            limit=100,
        )


async def test_messages_history_converts_dates_to_epoch_msec_and_formats_rows(service):
    result = await service.get_messages_history(
        date_from=datetime(2026, 9, 1, tzinfo=UTC),
        date_to=datetime(2026, 9, 2, 0, 0, 0, 500000, tzinfo=UTC),
        include_read=True,
        limit=50,
        timezone="Europe/Moscow",
    )

    assert result["data_status"] == "ok"
    assert result["row_count"] == 2
    assert result["include_read"] is True
    assert result["count_in_database"] == 3
    assert result["coverage"]["tz"] == "Europe/Moscow"
    first, second = result["items"]
    assert first["state"] == "unread"
    assert first["dt_iso"] == "2026-09-01T18:12:00.000+03:00"
    assert first["msg"].startswith("The robot 1381 will be restarted")
    assert second["state"] == "read"
    assert second["dt_iso"] == "2026-08-31T12:00:00.000+03:00"


async def test_messages_history_passes_epoch_msec_bounds_to_client(service):
    result = await service.get_messages_history(
        date_from=datetime(2026, 9, 1, tzinfo=UTC),
        date_to=datetime(2026, 9, 2, 0, 0, 0, 500000, tzinfo=UTC),
        raw=True,
    )

    assert result["raw_response"]["mint"] == 1788220800000
    assert result["raw_response"]["maxt"] == 1788307200500
    assert result["raw_response"]["read"] is False
    assert result["raw_response"]["limit"] == 100


async def test_messages_history_rejects_reversed_dates_and_missing_timezone(service):
    with pytest.raises(ValueError, match="later"):
        await service.get_messages_history(
            date_from=datetime(2026, 9, 2, tzinfo=UTC),
            date_to=datetime(2026, 9, 1, tzinfo=UTC),
        )
    with pytest.raises(ValueError, match="timezone"):
        await service.get_messages_history(
            date_from=datetime(2026, 9, 1),
            date_to=datetime(2026, 9, 2, tzinfo=UTC),
        )


async def test_previous_messages_converts_bound_and_formats_rows(service):
    result = await service.get_previous_messages(
        older_than=datetime(2026, 9, 1, 0, 0, 0, 500000, tzinfo=UTC),
        include_read=True,
        limit=50,
        timezone="Europe/Moscow",
        raw=True,
    )

    assert result["data_status"] == "ok"
    assert result["row_count"] == 1
    assert result["truncated"] is False
    assert result["include_read"] is True
    assert result["count_in_database"] == 1
    assert result["coverage"] == {"older_than": "2026-09-01T00:00:00.500000+00:00", "tz": "Europe/Moscow"}
    assert result["items"][0]["state"] == "read"
    assert result["items"][0]["dt_iso"] == "2026-08-31T12:00:00.000+03:00"
    assert result["raw_response"]["older_than"] == 1788220800500
    assert result["raw_response"]["read"] is True
    assert result["raw_response"]["limit"] == 50


async def test_previous_messages_reports_empty_page_and_hidden_read(service):
    result = await service.get_previous_messages(older_than=datetime(2026, 9, 1, tzinfo=UTC))

    assert result["data_status"] == "no_data_in_range"
    assert result["row_count"] == 0
    assert result["count_in_database"] is None
    assert "include_read=false" in result["notes"][0]
    assert "raw_response" not in result


async def test_previous_messages_rejects_bad_limit_and_missing_timezone(service):
    with pytest.raises(ValueError, match="limit"):
        await service.get_previous_messages(older_than=datetime(2026, 9, 1, tzinfo=UTC), limit=101)
    with pytest.raises(ValueError, match="timezone"):
        await service.get_previous_messages(older_than=datetime(2026, 9, 1))


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
        ("subscribe_messages", "subscribe_messages", {}),
        (
            "unsubscribe_messages",
            "unsubscribe_messages",
            {"subscription_id": "messages-sub-1"},
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
        ("get_messages_updates", "get_messages_updates"),
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
