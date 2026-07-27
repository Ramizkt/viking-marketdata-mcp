import asyncio
import json
import zlib
from unittest.mock import AsyncMock

import pytest

from app.viking_client import (
    VikingAPIError,
    VikingClient,
    VikingProtocolError,
    _Subscription,
)


def test_decode_plain_message():
    raw = json.dumps({"type": "x", "eid": "1", "data": {}})
    assert VikingClient.decode_messages(raw)[0]["eid"] == "1"


def test_decode_compressed_group():
    group = [{"eid": "1"}, {"eid": "2"}]
    compressed = zlib.compress(json.dumps(group).encode())
    assert [item["eid"] for item in VikingClient.decode_messages(compressed)] == ["1", "2"]


def test_extract_points_filters_period_and_bad_rows():
    response = {
        "data": {
            "values": [
                {"dt": 99, "v": 1},
                {"dt": 100, "v": 2},
                {"dt": 150, "v": 3},
                {"dt": 201, "v": 4},
                {"bad": True},
            ]
        }
    }
    assert VikingClient._extract_points(response, 100, 200) == {100: 2, 150: 3}


def test_parse_portfolio_list_preserves_all_documented_fields():
    response = {
        "data": {
            "portfolios_add": [["1", "alpha", "owner@example.com"]],
            "portfolios_del": [["2", "beta", "other@example.com"]],
        }
    }
    added, deleted = VikingClient._parse_portfolio_list_data(response)
    assert added == [
        {"robot_id": "1", "portfolio": "alpha", "owner": "owner@example.com"}
    ]
    assert deleted == [
        {"robot_id": "2", "portfolio": "beta", "owner": "other@example.com"}
    ]


@pytest.mark.parametrize(
    "data",
    [
        {"portfolios_add": "bad"},
        {"portfolios_add": [["1", "missing-owner"]]},
        {"portfolios_add": [["1", "name", 123]]},
    ],
)
def test_parse_portfolio_list_rejects_malformed_rows(data):
    with pytest.raises(VikingProtocolError):
        VikingClient._parse_portfolio_list_data({"data": data})


async def test_list_portfolios_accepts_normalized_subscription_rows():
    client = object.__new__(VikingClient)
    client.subscribe_available_portfolios = AsyncMock(
        return_value={
            "subscription_id": "sub-1",
            "portfolios_add": [
                {
                    "robot_id": "1",
                    "portfolio": "alpha",
                    "owner": "owner@example.com",
                }
            ],
        }
    )
    client.unsubscribe_available_portfolios = AsyncMock(return_value={"unsubscribed": True})
    client.request = AsyncMock(
        return_value={"data": {"portfolios": [["1", "alpha"]]}}
    )

    result = await client.list_portfolios()

    assert result == [
        {
            "robot_id": "1",
            "portfolio": "alpha",
            "owner": "owner@example.com",
            "history_available": True,
        }
    ]
    client.unsubscribe_available_portfolios.assert_awaited_once_with("sub-1")


async def test_subscribe_returns_complete_normalized_snapshot():
    client = object.__new__(VikingClient)
    client._subscriptions = {}
    client._subscribe = AsyncMock(
        return_value={
            "type": "available_portfolio_list.subscribe",
            "eid": "sub-1",
            "ts": 123,
            "r": "s",
            "data": {
                "portfolios_add": [["1", "alpha", "owner@example.com"]],
            },
        }
    )

    result = await client.subscribe_available_portfolios()

    assert result["subscription_id"] == "sub-1"
    assert result["r"] == "s"
    assert result["result"] == "s"
    assert result["data"] == {
        "portfolios_add": [
            {"robot_id": "1", "portfolio": "alpha", "owner": "owner@example.com"}
        ]
    }
    assert result["portfolios_add"] == result["data"]["portfolios_add"]
    assert result["portfolios_del"] == []


async def test_subscribe_rejects_malformed_snapshot_and_closes_connection():
    client = object.__new__(VikingClient)
    client._subscriptions = {
        "sub-1": _Subscription("available_portfolio_list.subscribe", asyncio.Queue())
    }
    client._subscribe = AsyncMock(
        return_value={
            "type": "available_portfolio_list.subscribe",
            "eid": "sub-1",
            "ts": 123,
            "r": "s",
            "data": {},
        }
    )
    client.close = AsyncMock()

    with pytest.raises(VikingProtocolError, match="missing portfolios_add"):
        await client.subscribe_available_portfolios()

    assert "sub-1" not in client._subscriptions
    client.close.assert_awaited_once()


async def test_get_portfolio_updates_returns_complete_events():
    client = object.__new__(VikingClient)
    queue = asyncio.Queue()
    await queue.put(
        {
            "type": "available_portfolio_list.subscribe",
            "eid": "sub-1",
            "ts": 123,
            "r": "u",
            "data": {
                "portfolios_add": [["1", "alpha", "owner@example.com"]],
                "portfolios_del": [["2", "beta", "other@example.com"]],
            },
        }
    )
    client._subscriptions = {
        "sub-1": _Subscription("available_portfolio_list.subscribe", queue)
    }

    result = await client.get_available_portfolio_updates("sub-1")

    assert result["event_count"] == 1
    assert result["events"][0]["ts"] == 123
    assert result["events"][0]["r"] == "u"
    assert result["events"][0]["data"]["portfolios_add"][0]["robot_id"] == "1"
    assert result["events"][0]["portfolios_add"][0]["owner"] == "owner@example.com"


async def test_get_portfolio_updates_raises_complete_api_error():
    client = object.__new__(VikingClient)
    queue = asyncio.Queue()
    response = {
        "type": "available_portfolio_list.subscribe",
        "eid": "sub-1",
        "ts": 123,
        "r": "e",
        "data": {"msg": "Operation timeout", "code": 666},
    }
    await queue.put(response)
    client._subscriptions = {
        "sub-1": _Subscription("available_portfolio_list.subscribe", queue)
    }

    with pytest.raises(VikingAPIError, match="Operation timeout") as error:
        await client.get_available_portfolio_updates("sub-1")

    assert error.value.code == 666
    assert error.value.response == response


async def test_get_available_portfolio_updates_rejects_overflowed_buffer():
    client = object.__new__(VikingClient)
    client._subscriptions = {
        "sub-1": _Subscription(
            "available_portfolio_list.subscribe",
            asyncio.Queue(),
            overflowed=True,
        )
    }

    with pytest.raises(VikingProtocolError, match="events were lost"):
        await client.get_available_portfolio_updates("sub-1")


async def test_get_portfolio_updates_rejects_unknown_subscription():
    client = object.__new__(VikingClient)
    client._subscriptions = {}
    with pytest.raises(ValueError, match="Unknown or inactive"):
        await client.get_available_portfolio_updates("missing")


async def test_unsubscribe_sends_subscription_eid_and_returns_complete_ack():
    client = object.__new__(VikingClient)
    client._subscriptions = {
        "sub-1": _Subscription("available_portfolio_list.subscribe", asyncio.Queue())
    }
    client.request = AsyncMock(
        return_value={
            "type": "available_portfolio_list.unsubscribe",
            "eid": "request-1",
            "ts": 456,
            "r": "p",
            "data": {},
        }
    )

    result = await client.unsubscribe_available_portfolios("sub-1")

    client.request.assert_awaited_once_with(
        "available_portfolio_list.unsubscribe",
        {"sub_eid": "sub-1"},
    )
    assert result == {
        "subscription_id": "sub-1",
        "type": "available_portfolio_list.unsubscribe",
        "eid": "request-1",
        "ts": 456,
        "r": "p",
        "result": "p",
        "data": {},
        "unsubscribed": True,
    }
    assert "sub-1" not in client._subscriptions


async def test_unsubscribe_api_error_keeps_subscription_for_retry():
    client = object.__new__(VikingClient)
    subscription = _Subscription(
        "available_portfolio_list.subscribe",
        asyncio.Queue(),
    )
    client._subscriptions = {"sub-1": subscription}
    client.request = AsyncMock(side_effect=VikingAPIError("Operation timeout", 666))

    with pytest.raises(VikingAPIError, match="Operation timeout"):
        await client.unsubscribe_available_portfolios("sub-1")

    assert client._subscriptions["sub-1"] is subscription


async def test_subscribe_portfolio_preserves_all_dynamic_snapshot_fields():
    client = object.__new__(VikingClient)
    client._subscriptions = {}
    client._subscribe = AsyncMock(
        return_value={
            "type": "portfolio.subscribe",
            "eid": "portfolio-sub-1",
            "ts": 789,
            "r": "s",
            "data": {
                "r_id": "1",
                "p_id": "alpha",
                "value": {
                    "name": "alpha",
                    "owner": "owner@example.com",
                    "buy": 101.5,
                    "custom_template_field": {"nested": [1, 2, 3]},
                    "uf0": {"v": 6, "c": "signal"},
                    "timetable": [{"begin": 36000, "end": 67200}],
                    "securities": {
                        "BTCUSDT": {
                            "sec_key": "BTCUSDT",
                            "pos": 2,
                            "custom_security_field": True,
                        }
                    },
                },
            },
        }
    )

    result = await client.subscribe_portfolio(robot_id="1", portfolio="alpha")

    client._subscribe.assert_awaited_once_with(
        "portfolio.subscribe",
        {"r_id": "1", "p_id": "alpha"},
    )
    assert result["subscription_id"] == "portfolio-sub-1"
    assert result["active"] is True
    assert result["robot_id"] == "1"
    assert result["portfolio"] == "alpha"
    assert result["value"]["custom_template_field"] == {"nested": [1, 2, 3]}
    assert result["value"]["uf0"] == {"v": 6, "c": "signal"}
    assert result["value"]["securities"]["BTCUSDT"]["custom_security_field"] is True
    assert result["data"]["value"] == result["value"]


async def test_subscribe_portfolio_rejects_mismatched_security_key_and_closes():
    client = object.__new__(VikingClient)
    client._subscriptions = {
        "portfolio-sub-1": _Subscription("portfolio.subscribe", asyncio.Queue())
    }
    client._subscribe = AsyncMock(
        return_value={
            "type": "portfolio.subscribe",
            "eid": "portfolio-sub-1",
            "ts": 789,
            "r": "s",
            "data": {
                "r_id": "1",
                "p_id": "alpha",
                "value": {
                    "name": "alpha",
                    "securities": {"BTCUSDT": {"sec_key": "ETHUSDT"}},
                },
            },
        }
    )
    client.close = AsyncMock()

    with pytest.raises(VikingProtocolError, match="does not match"):
        await client.subscribe_portfolio(robot_id="1", portfolio="alpha")

    assert "portfolio-sub-1" not in client._subscriptions
    client.close.assert_awaited_once()


async def test_get_portfolio_updates_preserves_partial_dynamic_fields():
    client = object.__new__(VikingClient)
    queue = asyncio.Queue()
    await queue.put(
        {
            "type": "portfolio.subscribe",
            "eid": "portfolio-sub-1",
            "ts": 790,
            "r": "u",
            "data": {
                "r_id": "1",
                "p_id": "alpha",
                "value": {
                    "name": "alpha",
                    "uf0": {"v": 7},
                    "securities": {
                        "BTCUSDT": {
                            "sec_key": "BTCUSDT",
                            "pos": 3,
                        }
                    },
                },
            },
        }
    )
    client._subscriptions = {
        "portfolio-sub-1": _Subscription(
            "portfolio.subscribe",
            queue,
            request_data={"r_id": "1", "p_id": "alpha"},
        )
    }

    result = await client.get_portfolio_updates("portfolio-sub-1")

    assert result["event_count"] == 1
    assert result["active"] is True
    assert result["events"][0]["value"]["uf0"] == {"v": 7}
    assert result["events"][0]["value"]["securities"]["BTCUSDT"]["pos"] == 3


async def test_get_portfolio_updates_marks_deleted_portfolio_inactive():
    client = object.__new__(VikingClient)
    queue = asyncio.Queue()
    await queue.put(
        {
            "type": "portfolio.subscribe",
            "eid": "portfolio-sub-1",
            "ts": 791,
            "r": "u",
            "data": {
                "r_id": "1",
                "p_id": "alpha",
                "value": {
                    "name": "alpha",
                    "owner": "owner@example.com",
                    "__action": "del",
                },
            },
        }
    )
    client._subscriptions = {
        "portfolio-sub-1": _Subscription(
            "portfolio.subscribe",
            queue,
            request_data={"r_id": "1", "p_id": "alpha"},
        )
    }

    result = await client.get_portfolio_updates("portfolio-sub-1")

    assert result["events"][0]["deleted"] is True
    assert result["active"] is False
    assert "portfolio-sub-1" not in client._subscriptions


async def test_get_portfolio_updates_raises_complete_api_error_and_deactivates():
    client = object.__new__(VikingClient)
    queue = asyncio.Queue()
    response = {
        "type": "portfolio.subscribe",
        "eid": "portfolio-sub-1",
        "ts": 791,
        "r": "e",
        "data": {"msg": "Permission denied", "code": 555},
    }
    await queue.put(response)
    client._subscriptions = {
        "portfolio-sub-1": _Subscription(
            "portfolio.subscribe",
            queue,
            request_data={"r_id": "1", "p_id": "alpha"},
        )
    }

    with pytest.raises(VikingAPIError, match="Permission denied") as error:
        await client.get_portfolio_updates("portfolio-sub-1")

    assert error.value.code == 555
    assert error.value.response == response
    assert "portfolio-sub-1" not in client._subscriptions


async def test_get_selected_portfolio_updates_rejects_overflowed_buffer():
    client = object.__new__(VikingClient)
    client._subscriptions = {
        "portfolio-sub-1": _Subscription(
            "portfolio.subscribe",
            asyncio.Queue(),
            overflowed=True,
            request_data={"r_id": "1", "p_id": "alpha"},
        )
    }

    with pytest.raises(VikingProtocolError, match="events were lost"):
        await client.get_portfolio_updates("portfolio-sub-1")


async def test_unsubscribe_portfolio_sends_subscription_eid_and_returns_ack():
    client = object.__new__(VikingClient)
    client._subscriptions = {
        "portfolio-sub-1": _Subscription("portfolio.subscribe", asyncio.Queue())
    }
    client.request = AsyncMock(
        return_value={
            "type": "portfolio.unsubscribe",
            "eid": "request-2",
            "ts": 792,
            "r": "p",
            "data": {},
        }
    )

    result = await client.unsubscribe_portfolio("portfolio-sub-1")

    client.request.assert_awaited_once_with(
        "portfolio.unsubscribe",
        {"sub_eid": "portfolio-sub-1"},
    )
    assert result["unsubscribed"] is True
    assert result["r"] == "p"
    assert "portfolio-sub-1" not in client._subscriptions


async def test_unsubscribe_portfolio_api_error_keeps_subscription_for_retry():
    client = object.__new__(VikingClient)
    subscription = _Subscription("portfolio.subscribe", asyncio.Queue())
    client._subscriptions = {"portfolio-sub-1": subscription}
    client.request = AsyncMock(side_effect=VikingAPIError("Operation timeout", 666))

    with pytest.raises(VikingAPIError, match="Operation timeout"):
        await client.unsubscribe_portfolio("portfolio-sub-1")

    assert client._subscriptions["portfolio-sub-1"] is subscription


async def test_get_current_portfolio_data_unsubscribes_immediately():
    client = object.__new__(VikingClient)
    client.subscribe_portfolio = AsyncMock(
        return_value={
            "subscription_id": "portfolio-sub-1",
            "active": True,
            "value": {"name": "alpha", "securities": {}},
        }
    )
    client.unsubscribe_portfolio = AsyncMock(
        return_value={"subscription_id": "portfolio-sub-1", "unsubscribed": True}
    )

    result = await client.get_current_portfolio_data(
        robot_id="1",
        portfolio="alpha",
    )

    client.subscribe_portfolio.assert_awaited_once_with(
        robot_id="1",
        portfolio="alpha",
    )
    client.unsubscribe_portfolio.assert_awaited_once_with("portfolio-sub-1")
    assert result["active"] is False
    assert result["unsubscribed"] is True
