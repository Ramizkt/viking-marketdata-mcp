from mcp.shared.memory import create_connected_server_and_client_session

from app import main


async def test_mcp_lists_expected_tools():
    async with create_connected_server_and_client_session(main.mcp, raise_exceptions=True) as session:
        result = await session.list_tools()
    tools = {tool.name: tool for tool in result.tools}
    assert set(tools) == {
        "list_available_portfolios",
        "subscribe_available_portfolios",
        "get_available_portfolio_updates",
        "unsubscribe_available_portfolios",
        "get_current_portfolio_data",
        "subscribe_portfolio",
        "get_portfolio_updates",
        "unsubscribe_portfolio",
        "get_portfolio_data",
    }
    assert tools["subscribe_available_portfolios"].annotations.idempotentHint is False
    assert tools["get_available_portfolio_updates"].annotations.idempotentHint is False
    assert tools["unsubscribe_available_portfolios"].annotations.idempotentHint is False
    assert tools["get_current_portfolio_data"].annotations.idempotentHint is True
    assert tools["subscribe_portfolio"].annotations.idempotentHint is False
    assert tools["get_portfolio_updates"].annotations.idempotentHint is False
    assert tools["unsubscribe_portfolio"].annotations.idempotentHint is False


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
        async def get_current_portfolio_data(self, *, robot_id: str, portfolio: str):
            return {
                "robot_id": robot_id,
                "portfolio": portfolio,
                "value": {
                    "name": portfolio,
                    "dynamic_field": {"nested": True},
                    "securities": {
                        "BTCUSDT": {
                            "sec_key": "BTCUSDT",
                            "custom_security_field": 123,
                        }
                    },
                },
                "unsubscribed": True,
            }

    monkeypatch.setattr(main, "_service_for_request", lambda: FakeService())

    async with create_connected_server_and_client_session(main.mcp, raise_exceptions=True) as session:
        result = await session.call_tool(
            "get_current_portfolio_data",
            {"robot_id": "1", "portfolio": "demo"},
        )

    assert result.isError is False
    assert result.structuredContent["value"]["dynamic_field"] == {"nested": True}
    security = result.structuredContent["value"]["securities"]["BTCUSDT"]
    assert security["custom_security_field"] == 123
