from mcp.shared.memory import create_connected_server_and_client_session

from app import main


async def test_mcp_lists_expected_tools():
    async with create_connected_server_and_client_session(main.mcp, raise_exceptions=True) as session:
        result = await session.list_tools()
    tools = {tool.name: tool for tool in result.tools}
    assert set(tools) == {
        "list_available_portfolios",
        "search_portfolios",
        "subscribe_available_portfolios",
        "get_available_portfolio_updates",
        "unsubscribe_available_portfolios",
        "get_portfolio_template",
        "get_current_portfolio_data",
        "get_robot_portfolio_trading_status",
        "subscribe_portfolio",
        "get_portfolio_updates",
        "unsubscribe_portfolio",
        "subscribe_portfolio_logs",
        "get_portfolio_log_updates",
        "unsubscribe_portfolio_logs",
        "subscribe_robot_logs",
        "get_robot_log_updates",
        "unsubscribe_robot_logs",
        "get_robot_log_history",
        "subscribe_portfolio_deals",
        "get_portfolio_deal_updates",
        "unsubscribe_portfolio_deals",
        "get_previous_portfolio_deals",
        "get_portfolio_deal_sec_keys",
        "get_portfolio_deal_history",
        "subscribe_data_connections",
        "get_data_connection_updates",
        "get_all_data_connections",
        "unsubscribe_data_connections",
        "get_transaction_connection",
        "get_transaction_connection_used_securities",
        "subscribe_transaction_connections",
        "get_transaction_connection_updates",
        "get_all_transaction_connections",
        "unsubscribe_transaction_connections",
        "subscribe_transaction_orders",
        "get_transaction_order_updates",
        "unsubscribe_transaction_orders",
        "subscribe_transaction_positions",
        "get_transaction_position_updates",
        "unsubscribe_transaction_positions",
        "get_robot_securities",
        "get_robot_client_codes",
        "find_security",
        "get_portfolio_data",
    }
    assert tools["subscribe_available_portfolios"].annotations.idempotentHint is False
    assert tools["get_available_portfolio_updates"].annotations.idempotentHint is False
    assert tools["unsubscribe_available_portfolios"].annotations.idempotentHint is False
    assert tools["get_current_portfolio_data"].annotations.idempotentHint is True
    assert tools["get_robot_portfolio_trading_status"].annotations.idempotentHint is True
    assert tools["get_robot_portfolio_trading_status"].annotations.readOnlyHint is True
    assert tools["subscribe_portfolio"].annotations.idempotentHint is False
    assert tools["get_portfolio_updates"].annotations.idempotentHint is False
    assert tools["unsubscribe_portfolio"].annotations.idempotentHint is False
    assert tools["subscribe_portfolio_logs"].annotations.idempotentHint is False
    assert tools["get_portfolio_log_updates"].annotations.idempotentHint is False
    assert tools["unsubscribe_portfolio_logs"].annotations.idempotentHint is False
    assert tools["subscribe_robot_logs"].annotations.idempotentHint is False
    assert tools["get_robot_log_updates"].annotations.idempotentHint is False
    assert tools["unsubscribe_robot_logs"].annotations.idempotentHint is False
    assert tools["get_robot_log_history"].annotations.idempotentHint is True
    assert tools["subscribe_portfolio_deals"].annotations.idempotentHint is False
    assert tools["get_portfolio_deal_updates"].annotations.idempotentHint is False
    assert tools["unsubscribe_portfolio_deals"].annotations.idempotentHint is False
    assert tools["get_previous_portfolio_deals"].annotations.idempotentHint is True
    assert tools["get_portfolio_deal_sec_keys"].annotations.idempotentHint is True
    assert tools["get_portfolio_deal_history"].annotations.idempotentHint is True
    assert tools["subscribe_data_connections"].annotations.idempotentHint is False
    assert tools["subscribe_transaction_connections"].annotations.idempotentHint is False
    assert tools["subscribe_transaction_orders"].annotations.idempotentHint is False
    assert tools["subscribe_transaction_positions"].annotations.idempotentHint is False
    assert tools["get_all_data_connections"].annotations.idempotentHint is True
    assert tools["get_transaction_connection_used_securities"].annotations.idempotentHint is True
    assert tools["get_previous_portfolio_deals"].inputSchema["properties"]["limit"]["maximum"] == 100
    assert tools["get_portfolio_deal_history"].inputSchema["properties"]["limit"]["maximum"] == 100_000
    deal_tools = (
        "subscribe_portfolio_deals",
        "get_portfolio_deal_updates",
        "get_previous_portfolio_deals",
        "get_portfolio_deal_history",
    )
    for tool_name in deal_tools:
        description = tools[tool_name].description or ""
        assert "aggr=true" in description
        assert "цена исходной заявки" in description
        assert "не фильтруй" in description
        assert "учитывать во всех подсчётах и расчётах" in description
    instructions = main.mcp._mcp_server.instructions or ""
    assert "aggr=true не фильтруй" in instructions
    assert "учитывать во всех подсчётах и расчётах" in instructions
    assert "такую запись также не исключай" in instructions
    history_schema = tools["get_robot_log_history"].inputSchema["properties"]
    message_filter_string = next(
        option
        for option in history_schema["message_filter"]["anyOf"]
        if option.get("type") == "string"
    )
    assert message_filter_string["maxLength"] == 256
    assert history_schema["limit"]["minimum"] == 1
    assert history_schema["limit"]["maximum"] == 100_000


async def test_mcp_portfolio_tool_returns_structured_content(monkeypatch):
    class FakeService:
        async def list_available_portfolios(self, *, history_only: bool):
            return {
                "count": 1,
                "history_only": history_only,
                "portfolios": [
                    {
                        "robot_id": "1",
                        "portfolio": "demo",
                        "owner": "owner@example.com",
                        "history_available": True,
                    }
                ],
            }

    monkeypatch.setattr(main, "_service_for_request", lambda: FakeService())

    async with create_connected_server_and_client_session(main.mcp, raise_exceptions=True) as session:
        result = await session.call_tool(
            "list_available_portfolios",
            {"history_only": True},
        )

    assert result.isError is False
    assert result.structuredContent["count"] == 1
    assert result.structuredContent["portfolios"][0]["portfolio"] == "demo"


async def test_mcp_current_portfolio_data_returns_all_fields(monkeypatch):
    class FakeService:
        async def get_current_portfolio_data(
            self, *, robot_id: str, portfolio: str, raw: bool = False
        ):
            return {
                "data_status": "ok",
                "row_count": 1,
                "truncated": False,
                "coverage": None,
                "notes": [],
                "robot_id": robot_id,
                "portfolio": portfolio,
                "subscription_closed": True,
                "items": [
                    {
                        "name": portfolio,
                        "dynamic_field": {"nested": True},
                        "securities": {
                            "BTCUSDT": {
                                "sec_key": "BTCUSDT",
                                "custom_security_field": 123,
                            }
                        },
                    }
                ],
            }

    monkeypatch.setattr(main, "_service_for_request", lambda: FakeService())

    async with create_connected_server_and_client_session(main.mcp, raise_exceptions=True) as session:
        result = await session.call_tool(
            "get_current_portfolio_data",
            {"robot_id": "1", "portfolio": "demo"},
        )

    assert result.isError is False
    assert result.structuredContent["items"][0]["dynamic_field"] == {"nested": True}
    security = result.structuredContent["items"][0]["securities"]["BTCUSDT"]
    assert security["custom_security_field"] == 123


async def test_mcp_portfolio_template_returns_complete_schema(monkeypatch):
    class FakeService:
        async def get_portfolio_template(self, *, robot_id: str, portfolio: str):
            return {
                "robot_id": robot_id,
                "portfolio": portfolio,
                "template_id": "portfolio_viking_base",
                "template": {
                    "template_id": "portfolio_viking_base",
                    "template_fields": {
                        "portfolio": [{"field": "uf0"}],
                        "security": [{"field": "pos"}],
                        "custom_group": [{"field": "custom"}],
                    },
                },
                "template_fields": {
                    "portfolio": [{"field": "uf0"}],
                    "security": [{"field": "pos"}],
                    "custom_group": [{"field": "custom"}],
                },
            }

    monkeypatch.setattr(main, "_service_for_request", lambda: FakeService())

    async with create_connected_server_and_client_session(
        main.mcp, raise_exceptions=True
    ) as session:
        result = await session.call_tool(
            "get_portfolio_template",
            {"robot_id": "1", "portfolio": "demo"},
        )

    assert result.isError is False
    assert result.structuredContent["template_id"] == "portfolio_viking_base"
    assert result.structuredContent["template_fields"]["custom_group"] == [
        {"field": "custom"}
    ]


async def test_mcp_subscribe_portfolio_logs_returns_complete_snapshot(monkeypatch):
    class FakeService:
        async def subscribe_portfolio_logs(self, *, robot_id: str, portfolio: str):
            return {
                "subscription_id": "portfolio-logs-sub-1",
                "active": True,
                "robot_id": robot_id,
                "portfolio": portfolio,
                "max_time": 123,
                "log_count": 1,
                "logs": [
                    {
                        "level": 1,
                        "msg": "test",
                        "t": 100,
                        "dt": 101,
                        "dynamic": True,
                    }
                ],
            }

    monkeypatch.setattr(main, "_service_for_request", lambda: FakeService())

    async with create_connected_server_and_client_session(
        main.mcp, raise_exceptions=True
    ) as session:
        result = await session.call_tool(
            "subscribe_portfolio_logs",
            {"robot_id": "1", "portfolio": "demo"},
        )

    assert result.isError is False
    assert result.structuredContent["subscription_id"] == "portfolio-logs-sub-1"
    assert result.structuredContent["logs"][0]["dynamic"] is True


async def test_mcp_robot_log_history_returns_structured_content(monkeypatch):
    class FakeService:
        async def get_robot_log_history(
            self,
            *,
            robot_id,
            date_from,
            date_to,
            message_filter,
            limit,
            verbosity="compact",
            timezone="Europe/Moscow",
            raw=False,
        ):
            return {
                "data_status": "ok",
                "row_count": 1,
                "truncated": False,
                "coverage": {
                    "from": date_from.isoformat(),
                    "to": date_to.isoformat(),
                    "tz": timezone,
                },
                "notes": [],
                "robot_id": robot_id,
                "verbosity": verbosity,
                "items": [
                    {
                        "dt": "1677586103000245321",
                        "dt_iso": "2023-02-28T15:08:23.000+03:00",
                        "event_type": "log",
                    }
                ],
            }

    monkeypatch.setattr(main, "_service_for_request", lambda: FakeService())

    async with create_connected_server_and_client_session(
        main.mcp, raise_exceptions=True
    ) as session:
        result = await session.call_tool(
            "get_robot_log_history",
            {
                "robot_id": "1",
                "date_from": "2026-01-01T00:00:00Z",
                "date_to": "2026-01-01T00:01:00Z",
                "message_filter": "*test*",
                "limit": 100,
            },
        )

    assert result.isError is False
    assert result.structuredContent["row_count"] == 1
    assert result.structuredContent["verbosity"] == "compact"
