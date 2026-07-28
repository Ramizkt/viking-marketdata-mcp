import asyncio
import json
import zlib
from types import SimpleNamespace
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


async def test_get_template_id_sends_portfolio_object_and_preserves_response():
    client = object.__new__(VikingClient)
    client.request = AsyncMock(
        return_value={
            "type": "get_template_id",
            "eid": "request-template-id",
            "ts": 800,
            "r": "p",
            "data": {"template_id": "portfolio_viking_base"},
        }
    )

    result = await client.get_template_id(
        view="portfolio",
        object_id={"r_id": "1", "p_id": "alpha"},
    )

    client.request.assert_awaited_once_with(
        "get_template_id",
        {
            "view": "portfolio",
            "id": {"r_id": "1", "p_id": "alpha"},
        },
    )
    assert result["template_id"] == "portfolio_viking_base"
    assert result["data"] == {"template_id": "portfolio_viking_base"}
    assert result["result"] == "p"


async def test_get_template_by_id_preserves_complete_template():
    client = object.__new__(VikingClient)
    template = {
        "_comment": "dynamic template",
        "template_id": "portfolio_viking_base",
        "template_fields": {
            "portfolio": [{"field": "uf0", "formatter": "number"}],
            "security": [{"field": "pos", "formatter": "integer"}],
            "custom_group": [{"field": "custom", "nested": {"x": 1}}],
        },
    }
    client.request = AsyncMock(
        return_value={
            "type": "get_template_by_id",
            "eid": "request-template",
            "ts": 801,
            "r": "p",
            "data": {"template": template},
        }
    )

    result = await client.get_template_by_id(
        template_id="portfolio_viking_base"
    )

    client.request.assert_awaited_once_with(
        "get_template_by_id",
        {"template_id": "portfolio_viking_base"},
    )
    assert result["template"] == template
    assert result["template_fields"]["custom_group"][0]["nested"] == {"x": 1}
    assert result["requested_template_id"] == "portfolio_viking_base"


async def test_get_portfolio_template_resolves_id_before_template():
    client = object.__new__(VikingClient)
    client.get_template_id = AsyncMock(
        return_value={
            "type": "get_template_id",
            "eid": "request-template-id",
            "ts": 800,
            "r": "p",
            "result": "p",
            "data": {"template_id": "portfolio_viking_base"},
            "template_id": "portfolio_viking_base",
        }
    )
    client.get_template_by_id = AsyncMock(
        return_value={
            "type": "get_template_by_id",
            "eid": "request-template",
            "ts": 801,
            "r": "p",
            "result": "p",
            "data": {},
            "requested_template_id": "portfolio_viking_base",
            "template_id": "portfolio_viking_base",
            "template": {
                "template_id": "portfolio_viking_base",
                "template_fields": {"portfolio": [{"field": "uf0"}]},
            },
            "template_fields": {"portfolio": [{"field": "uf0"}]},
        }
    )

    result = await client.get_portfolio_template(
        robot_id="1",
        portfolio="alpha",
    )

    client.get_template_id.assert_awaited_once_with(
        view="portfolio",
        object_id={"r_id": "1", "p_id": "alpha"},
    )
    client.get_template_by_id.assert_awaited_once_with(
        template_id="portfolio_viking_base"
    )
    assert result["template_id"] == "portfolio_viking_base"
    assert result["template_fields"]["portfolio"][0]["field"] == "uf0"
    assert result["get_template_by_id_response"]["returned_template_id"] == (
        "portfolio_viking_base"
    )


async def test_get_template_id_rejects_malformed_success_response():
    client = object.__new__(VikingClient)
    client.request = AsyncMock(
        return_value={
            "type": "get_template_id",
            "eid": "request-template-id",
            "ts": 800,
            "r": "p",
            "data": {},
        }
    )

    with pytest.raises(VikingProtocolError, match="template_id"):
        await client.get_template_id(
            view="portfolio",
            object_id={"r_id": "1", "p_id": "alpha"},
        )


async def test_subscribe_portfolio_logs_uses_documented_identity_and_preserves_fields():
    client = object.__new__(VikingClient)
    client._subscriptions = {}
    client._subscribe = AsyncMock(
        return_value={
            "type": "portfolio_logs.subscribe",
            "eid": "portfolio-logs-sub-1",
            "ts": 900,
            "r": "s",
            "data": {
                "mt": 1671194446799559398,
                "r_id": "1",
                "p_id": "alpha",
                "values": [
                    {
                        "level": 5,
                        "name": "alpha",
                        "owner": "",
                        "msg": "with owner",
                        "t": 1671194458000035338,
                        "dt": 1671194458000994686,
                        "custom": {"preserved": True},
                    }
                ],
            },
        }
    )

    result = await client.subscribe_portfolio_logs(robot_id="1", portfolio="alpha")

    client._subscribe.assert_awaited_once_with(
        "portfolio_logs.subscribe",
        {"r_id": "1", "p_id": "alpha"},
    )
    assert result["subscription_id"] == "portfolio-logs-sub-1"
    assert result["active"] is True
    assert result["robot_id"] == "1"
    assert result["portfolio"] == "alpha"
    assert result["max_time"] == 1671194446799559398
    assert result["logs"][0]["custom"] == {"preserved": True}


async def test_subscribe_portfolio_logs_rejects_mismatched_portfolio_and_closes():
    client = object.__new__(VikingClient)
    client._subscriptions = {
        "portfolio-logs-sub-1": _Subscription(
            "portfolio_logs.subscribe",
            asyncio.Queue(),
        )
    }
    client._subscribe = AsyncMock(
        return_value={
            "type": "portfolio_logs.subscribe",
            "eid": "portfolio-logs-sub-1",
            "ts": 900,
            "r": "s",
            "data": {
                "mt": 1,
                "r_id": "1",
                "p_id": "other",
                "values": [],
            },
        }
    )
    client.close = AsyncMock()

    with pytest.raises(VikingProtocolError, match="p_id"):
        await client.subscribe_portfolio_logs(robot_id="1", portfolio="alpha")

    assert "portfolio-logs-sub-1" not in client._subscriptions
    client.close.assert_awaited_once()


@pytest.mark.parametrize(
    ("method_name", "kwargs"),
    [
        (
            "subscribe_portfolio_logs",
            {"robot_id": "1", "portfolio": "alpha"},
        ),
        ("subscribe_robot_logs", {"robot_id": "1"}),
    ],
)
async def test_log_subscribe_preserves_complete_api_error(method_name, kwargs):
    client = object.__new__(VikingClient)
    error = VikingAPIError(
        "Permission denied",
        555,
        response={
            "type": "robot_logs.subscribe",
            "eid": "logs-sub-1",
            "ts": 900,
            "r": "e",
            "data": {"msg": "Permission denied", "code": 555},
        },
    )
    client._subscribe = AsyncMock(side_effect=error)

    with pytest.raises(VikingAPIError, match="Permission denied") as raised:
        await getattr(client, method_name)(**kwargs)

    assert raised.value.code == 555
    assert raised.value.response == error.response


async def test_subscribe_robot_logs_accepts_documented_snapshot():
    client = object.__new__(VikingClient)
    client._subscriptions = {}
    client._subscribe = AsyncMock(
        return_value={
            "type": "robot_logs.subscribe",
            "eid": "robot-logs-sub-1",
            "ts": 901,
            "r": "s",
            "data": {
                "mt": 1671195116000769082,
                "r_id": "1",
                "values": [
                    {
                        "level": 5,
                        "name": "",
                        "owner": "1",
                        "msg": "without name",
                        "t": 1671195119000062295,
                        "dt": 1671195119001430926,
                        "r_id": "1",
                    }
                ],
            },
        }
    )

    result = await client.subscribe_robot_logs(robot_id="1")

    client._subscribe.assert_awaited_once_with(
        "robot_logs.subscribe",
        {"r_id": "1"},
    )
    assert result["subscription_id"] == "robot-logs-sub-1"
    assert result["log_count"] == 1
    assert result["logs"][0]["r_id"] == "1"


async def test_get_robot_log_updates_accepts_rows_omitted_by_api_example():
    client = object.__new__(VikingClient)
    queue = asyncio.Queue()
    await queue.put(
        {
            "type": "robot_logs.subscribe",
            "eid": "robot-logs-sub-1",
            "ts": 902,
            "r": "u",
            "data": {
                "r_id": "1",
                "values": [
                    {
                        "level": 2,
                        "owner": None,
                        "msg": "calculation warning",
                        "t": 1671195121000031284,
                        "dt": 1671195121001134808,
                        "dynamic": ["kept"],
                    }
                ],
            },
        }
    )
    client._subscriptions = {
        "robot-logs-sub-1": _Subscription(
            "robot_logs.subscribe",
            queue,
            request_data={"r_id": "1"},
        )
    }

    result = await client.get_robot_log_updates("robot-logs-sub-1")

    assert result["event_count"] == 1
    assert result["active"] is True
    assert result["events"][0]["logs"][0]["dynamic"] == ["kept"]


async def test_get_robot_log_updates_rejects_malformed_required_log_field():
    client = object.__new__(VikingClient)
    queue = asyncio.Queue()
    await queue.put(
        {
            "type": "robot_logs.subscribe",
            "eid": "robot-logs-sub-1",
            "ts": 902,
            "r": "u",
            "data": {
                "r_id": "1",
                "values": [
                    {
                        "level": 2,
                        "msg": "missing t",
                        "dt": 1671195121001134808,
                    }
                ],
            },
        }
    )
    client._subscriptions = {
        "robot-logs-sub-1": _Subscription(
            "robot_logs.subscribe",
            queue,
            request_data={"r_id": "1"},
        )
    }
    client.close = AsyncMock()

    with pytest.raises(VikingProtocolError, match="'t'"):
        await client.get_robot_log_updates("robot-logs-sub-1")

    assert "robot-logs-sub-1" not in client._subscriptions
    client.close.assert_awaited_once()


async def test_get_portfolio_log_updates_raises_api_error_and_deactivates():
    client = object.__new__(VikingClient)
    queue = asyncio.Queue()
    response = {
        "type": "portfolio_logs.subscribe",
        "eid": "portfolio-logs-sub-1",
        "ts": 903,
        "r": "e",
        "data": {"msg": "Permission denied", "code": 555},
    }
    await queue.put(response)
    client._subscriptions = {
        "portfolio-logs-sub-1": _Subscription(
            "portfolio_logs.subscribe",
            queue,
            request_data={"r_id": "1", "p_id": "alpha"},
        )
    }

    with pytest.raises(VikingAPIError, match="Permission denied") as error:
        await client.get_portfolio_log_updates("portfolio-logs-sub-1")

    assert error.value.code == 555
    assert error.value.response == response
    assert "portfolio-logs-sub-1" not in client._subscriptions


async def test_get_robot_log_updates_rejects_overflowed_buffer():
    client = object.__new__(VikingClient)
    client._subscriptions = {
        "robot-logs-sub-1": _Subscription(
            "robot_logs.subscribe",
            asyncio.Queue(),
            overflowed=True,
            request_data={"r_id": "1"},
        )
    }

    with pytest.raises(VikingProtocolError, match="events were lost"):
        await client.get_robot_log_updates("robot-logs-sub-1")


@pytest.mark.parametrize(
    ("subscribe_type", "unsubscribe_type", "method_name"),
    [
        (
            "portfolio_logs.subscribe",
            "portfolio_logs.unsubscribe",
            "unsubscribe_portfolio_logs",
        ),
        (
            "robot_logs.subscribe",
            "robot_logs.unsubscribe",
            "unsubscribe_robot_logs",
        ),
    ],
)
async def test_log_unsubscribe_uses_subscription_eid(
    subscribe_type,
    unsubscribe_type,
    method_name,
):
    client = object.__new__(VikingClient)
    client._subscriptions = {
        "logs-sub-1": _Subscription(subscribe_type, asyncio.Queue())
    }
    client.request = AsyncMock(
        return_value={
            "type": unsubscribe_type,
            "eid": "unsubscribe-request-1",
            "ts": 904,
            "r": "p",
            "data": {},
        }
    )

    result = await getattr(client, method_name)("logs-sub-1")

    client.request.assert_awaited_once_with(
        unsubscribe_type,
        {"sub_eid": "logs-sub-1"},
    )
    assert result["unsubscribed"] is True
    assert result["subscription_id"] == "logs-sub-1"
    assert "logs-sub-1" not in client._subscriptions


async def test_log_unsubscribe_api_error_keeps_subscription_for_retry():
    client = object.__new__(VikingClient)
    subscription = _Subscription("robot_logs.subscribe", asyncio.Queue())
    client._subscriptions = {"robot-logs-sub-1": subscription}
    client.request = AsyncMock(side_effect=VikingAPIError("Operation timeout", 666))

    with pytest.raises(VikingAPIError, match="Operation timeout"):
        await client.unsubscribe_robot_logs("robot-logs-sub-1")

    assert client._subscriptions["robot-logs-sub-1"] is subscription


async def test_log_unsubscribe_unexpected_result_keeps_subscription():
    client = object.__new__(VikingClient)
    subscription = _Subscription("robot_logs.subscribe", asyncio.Queue())
    client._subscriptions = {"robot-logs-sub-1": subscription}
    client.request = AsyncMock(
        return_value={
            "type": "robot_logs.unsubscribe",
            "eid": "unsubscribe-request-1",
            "ts": 904,
            "r": "u",
            "data": {},
        }
    )

    with pytest.raises(VikingProtocolError, match="expected r='p'"):
        await client.unsubscribe_robot_logs("robot-logs-sub-1")

    assert client._subscriptions["robot-logs-sub-1"] is subscription


async def test_get_robot_log_history_sends_epoch_nsec_strings_and_preserves_rows():
    client = object.__new__(VikingClient)
    client.request = AsyncMock(
        return_value={
            "type": "robot_logs.get_history",
            "eid": "history-request-1",
            "ts": 905,
            "r": "p",
            "data": {
                "values": [
                    {
                        "dt": "1677586103000245321",
                        "r_id": "1",
                        "name": "alpha",
                        "level": 1,
                        "msg": 'Compilation on "alpha" is OK',
                        "owner": "",
                        "dynamic": {"preserved": True},
                    }
                ]
            },
        }
    )

    result = await client.get_robot_log_history(
        robot_id="1",
        mint_ns="1677586000000000000",
        maxt_ns="1677587000000000000",
        message_filter="*alpha*",
        limit=100,
    )

    client.request.assert_awaited_once_with(
        "robot_logs.get_history",
        {
            "r_id": "1",
            "mint": "1677586000000000000",
            "maxt": "1677587000000000000",
            "lim": 100,
            "msg": "*alpha*",
        },
    )
    assert result["result"] == "p"
    assert result["log_count"] == 1
    assert result["logs"][0]["dt"] == "1677586103000245321"
    assert result["logs"][0]["dynamic"] == {"preserved": True}


async def test_get_robot_log_history_rejects_mismatched_row_robot_id():
    client = object.__new__(VikingClient)
    client.request = AsyncMock(
        return_value={
            "type": "robot_logs.get_history",
            "eid": "history-request-1",
            "ts": 905,
            "r": "p",
            "data": {
                "values": [
                    {
                        "dt": 1677586103000245321,
                        "r_id": "2",
                        "name": "alpha",
                        "level": 1,
                        "msg": "wrong robot",
                    }
                ]
            },
        }
    )

    with pytest.raises(VikingProtocolError, match="r_id"):
        await client.get_robot_log_history(
            robot_id="1",
            mint_ns="1",
            maxt_ns="2000000000000000000",
        )


async def test_get_robot_log_history_rejects_unexpected_result():
    client = object.__new__(VikingClient)
    client.request = AsyncMock(
        return_value={
            "type": "robot_logs.get_history",
            "eid": "history-request-1",
            "ts": 905,
            "r": "u",
            "data": {"values": []},
        }
    )

    with pytest.raises(VikingProtocolError, match="expected r='p'"):
        await client.get_robot_log_history(
            robot_id="1",
            mint_ns="1",
            maxt_ns="2",
        )


async def test_get_robot_log_history_rejects_invalid_bounds_and_filter():
    client = object.__new__(VikingClient)

    with pytest.raises(ValueError, match="digit string"):
        await client.get_robot_log_history(
            robot_id="1",
            mint_ns="2026-01-01",
            maxt_ns="2",
        )
    with pytest.raises(ValueError, match="256"):
        await client.get_robot_log_history(
            robot_id="1",
            mint_ns="1",
            maxt_ns="2",
            message_filter="x" * 257,
        )


def _deal(**overrides):
    row = {
        "id": "deal-1",
        "ono": 0,
        "price": 101.25,
        "orig_price": 101.5,
        "buy_sell": 1,
        "quantity": 2,
        "cn": "virtual",
        "sec": "BTC",
        "decimals": 2,
        "dt": "1676360033000144435",
        "curpos": 2,
        "lot_size": 1,
    }
    row.update(overrides)
    return row


async def test_subscribe_portfolio_deals_uses_documented_payload_and_preserves_fields():
    client = object.__new__(VikingClient)
    client._subscriptions = {}
    client.close = AsyncMock()
    client._subscribe = AsyncMock(return_value={
        "type": "portfolio_deals.subscribe",
        "eid": "deals-sub-1",
        "ts": 1,
        "r": "s",
        "data": {
            "mt": "1676360033000144435",
            "r_id": "1",
            "p_id": "arb",
            "values": [_deal(custom={"kept": True})],
        },
    })

    result = await client.subscribe_portfolio_deals(robot_id="1", portfolio="arb")

    client._subscribe.assert_awaited_once_with(
        "portfolio_deals.subscribe", {"r_id": "1", "p_id": "arb"}
    )
    assert result["deal_count"] == 1
    assert result["deals"][0]["custom"] == {"kept": True}


async def test_portfolio_deals_history_sends_string_bounds_and_security_filter():
    client = object.__new__(VikingClient)
    client.request = AsyncMock(return_value={
        "type": "portfolio_deals.get_history",
        "eid": "request-1",
        "ts": 2,
        "r": "p",
        "data": {"values": [_deal(r_id="1", name="arb")]},
    })

    result = await client.get_portfolio_deal_history(
        robot_id="1",
        portfolio="arb",
        mint_ns="1",
        maxt_ns="2000000000000000000",
        security_key="BTC",
        limit=10,
    )

    client.request.assert_awaited_once_with(
        "portfolio_deals.get_history",
        {
            "r_id": "1",
            "p_id": "arb",
            "mint": "1",
            "maxt": "2000000000000000000",
            "lim": 10,
            "sec_key": "BTC",
        },
    )
    assert result["deal_count"] == 1


async def test_previous_portfolio_deals_enforces_documented_limit():
    client = object.__new__(VikingClient)
    with pytest.raises(ValueError, match="1..100"):
        await client.get_previous_portfolio_deals(
            robot_id="1", portfolio="arb", before_ns="2", limit=101
        )


async def test_get_portfolio_deal_sec_keys_parses_unique_instruments():
    client = object.__new__(VikingClient)
    client.request = AsyncMock(return_value={
        "type": "portfolio_deals.get_sec_keys",
        "eid": "request-2",
        "ts": 3,
        "r": "p",
        "data": {"values": ["BTC", "ETH"]},
    })

    result = await client.get_portfolio_deal_sec_keys(
        robot_id="1", portfolio="arb"
    )

    assert result["security_keys"] == ["BTC", "ETH"]
    assert result["security_count"] == 2


async def test_portfolio_deals_unsubscribe_uses_subscription_eid():
    client = object.__new__(VikingClient)
    client._subscriptions = {
        "deals-sub-1": _Subscription("portfolio_deals.subscribe", asyncio.Queue())
    }
    client.request = AsyncMock(return_value={
        "type": "portfolio_deals.unsubscribe",
        "eid": "request-3",
        "ts": 4,
        "r": "p",
        "data": {},
    })

    result = await client.unsubscribe_portfolio_deals("deals-sub-1")

    client.request.assert_awaited_once_with(
        "portfolio_deals.unsubscribe", {"sub_eid": "deals-sub-1"}
    )
    assert result["unsubscribed"] is True


async def test_get_used_securities_uses_real_wire_type_despite_api_table_typo():
    client = object.__new__(VikingClient)
    client.request = AsyncMock(return_value={
        "type": "trans_conn.get_used_secs",
        "eid": "request-1",
        "ts": 10,
        "r": "p",
        "data": {
            "contracts": {
                "VT_BTCUSD": {
                    "sec_key": "VT_BTCUSD",
                    "step": 0.5,
                    "sec_key_subscr": "1",
                    "sec_code": "BTC/USD",
                    "coin": "",
                    "bid": 35000,
                    "offer": 36000,
                    "decimals": 1,
                }
            }
        },
    })

    result = await client.get_transaction_connection_used_securities(
        robot_id="1", sec_type=67108864, name="aws"
    )

    client.request.assert_awaited_once_with(
        "trans_conn.get_used_secs",
        {"r_id": "1", "conn": {"sec_type": 67108864, "name": "aws"}},
    )
    assert result["security_count"] == 1


async def test_data_connection_list_preserves_server_defined_fields():
    client = object.__new__(VikingClient)
    client.request = AsyncMock(return_value={
        "type": "data_conn.get_all",
        "eid": "request-2",
        "ts": 11,
        "r": "p",
        "data": {
            "r_id": "1",
            "values": {
                "3_FAST BestPrices": {
                    "sec_type": 3,
                    "name": "FAST BestPrices",
                    "disabled": False,
                    "stream_state": {"BestPrices": 2},
                    "custom": {"kept": True},
                }
            },
        },
    })

    result = await client.get_all_data_connections(robot_id="1")

    client.request.assert_awaited_once_with("data_conn.get_all", {"r_id": "1"})
    assert result["connection_count"] == 1
    assert result["connections"]["3_FAST BestPrices"]["custom"] == {"kept": True}


async def test_transaction_order_subscription_uses_connection_pair_and_parses_delete():
    client = object.__new__(VikingClient)
    client._subscriptions = {}
    client.close = AsyncMock()
    client._subscribe = AsyncMock(return_value={
        "type": "trans_conn_orders.subscribe",
        "eid": "orders-sub-1",
        "ts": 12,
        "r": "s",
        "data": {
            "r_id": "1",
            "value": {
                "sec_type": 1048576,
                "name": "roma",
                "full_name": "bitmex_send_roma",
                "disabled": False,
                "active_orders": {},
            },
        },
    })

    result = await client.subscribe_transaction_orders(
        robot_id="1", sec_type=1048576, name="roma"
    )

    client._subscribe.assert_awaited_once_with(
        "trans_conn_orders.subscribe",
        {"r_id": "1", "conn": {"sec_type": 1048576, "name": "roma"}},
    )
    assert result["active"] is True
    assert result["value"]["active_orders"] == {}


async def test_connection_subscription_accepts_repeated_snapshot_and_update():
    client = object.__new__(VikingClient)
    queue = asyncio.Queue()
    await queue.put({
        "type": "trans_conn.subscribe",
        "eid": "conn-sub-1",
        "ts": 13,
        "r": "u",
        "data": {
            "r_id": "1",
            "values": {
                "0_virtual": {
                    "sec_type": 0,
                    "name": "virtual",
                    "stream_state": {"TRANS": 2},
                }
            },
        },
    })
    await queue.put({
        "type": "trans_conn.subscribe",
        "eid": "conn-sub-1",
        "ts": 14,
        "r": "s",
        "data": {
            "r_id": "1",
            "values": {
                "0_virtual": {
                    "sec_type": 0,
                    "name": "virtual",
                    "disabled": False,
                }
            },
        },
    })
    client._subscriptions = {
        "conn-sub-1": _Subscription(
            "trans_conn.subscribe", queue, request_data={"r_id": "1"}
        )
    }

    result = await client.get_transaction_connection_updates("conn-sub-1")

    assert [event["r"] for event in result["events"]] == ["u", "s"]
    assert result["event_count"] == 2


async def test_connection_unsubscribe_uses_subscription_eid():
    client = object.__new__(VikingClient)
    client._subscriptions = {
        "poses-sub-1": _Subscription("trans_conn_poses.subscribe", asyncio.Queue())
    }
    client.request = AsyncMock(return_value={
        "type": "trans_conn_poses.unsubscribe",
        "eid": "request-3",
        "ts": 15,
        "r": "p",
        "data": {},
    })

    result = await client.unsubscribe_transaction_positions("poses-sub-1")

    client.request.assert_awaited_once_with(
        "trans_conn_poses.unsubscribe", {"sub_eid": "poses-sub-1"}
    )
    assert result["unsubscribed"] is True


async def test_get_robot_securities_collects_all_next_pages():
    client = object.__new__(VikingClient)
    queue = asyncio.Queue()
    await queue.put({
        "type": "robot.get_securities",
        "eid": "secs-1",
        "ts": 17,
        "r": "p",
        "data": {
            "next": False,
            "securities": {"ETH": {"sec_key": "ETH", "custom": 2}},
        },
    })
    client._subscriptions = {
        "secs-1": _Subscription("robot.get_securities", queue)
    }
    client.settings = SimpleNamespace(viking_request_timeout_seconds=1)
    client._subscribe = AsyncMock(return_value={
        "type": "robot.get_securities",
        "eid": "secs-1",
        "ts": 16,
        "r": "p",
        "data": {
            "next": True,
            "securities": {"BTC": {"sec_key": "BTC", "custom": 1}},
        },
    })

    result = await client.get_robot_securities(
        robot_id="1", reload=True, sec_type=3
    )

    client._subscribe.assert_awaited_once_with(
        "robot.get_securities", {"r_id": "1", "reload": True, "sec_type": 3}
    )
    assert result["page_count"] == 2
    assert result["security_count"] == 2
    assert result["securities"]["ETH"]["custom"] == 2
    assert "secs-1" not in client._subscriptions


async def test_get_robot_client_codes_preserves_dynamic_fields():
    client = object.__new__(VikingClient)
    client.request = AsyncMock(return_value={
        "type": "robot.get_client_codes",
        "eid": "codes-1",
        "ts": 18,
        "r": "p",
        "data": {
            "r_id": "1",
            "values": [{"sec_type": 1048576, "ll": "bitmex_send_x/y", "extra": 1}],
        },
    })

    result = await client.get_robot_client_codes(robot_id="1")

    client.request.assert_awaited_once_with(
        "robot.get_client_codes", {"r_id": "1"}
    )
    assert result["client_code_count"] == 1
    assert result["client_codes"][0]["extra"] == 1


async def test_find_security_sends_optional_scope_and_preserves_formula_details():
    client = object.__new__(VikingClient)
    client.request = AsyncMock(return_value={
        "type": "robot.find_security",
        "eid": "find-1",
        "ts": 19,
        "r": "p",
        "data": {
            "key": "BTC",
            "portfolios": [{"r_id": "1", "p_id": "arb", "disabled": False}],
            "formulas": [{
                "r_id": "1", "p_id": "arb", "pos": 4, "text": "BTC",
                "sec": "", "title": "Field", "field": "uf1",
                "value": "BTC", "disabled": False,
            }],
        },
    })

    result = await client.find_security(
        security_key="BTC", robot_id="1", portfolio="arb"
    )

    client.request.assert_awaited_once_with(
        "robot.find_security", {"key": "BTC", "r_id": "1", "p_id": "arb"}
    )
    assert result["portfolio_count"] == 1
    assert result["formula_match_count"] == 1
    assert result["formulas"][0]["field"] == "uf1"
