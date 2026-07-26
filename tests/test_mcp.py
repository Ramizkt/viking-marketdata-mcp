from mcp.shared.memory import create_connected_server_and_client_session

from app import main


async def test_mcp_lists_expected_tools():
    async with create_connected_server_and_client_session(main.mcp, raise_exceptions=True) as session:
        result = await session.list_tools()
    assert {tool.name for tool in result.tools} == {
        "credential_setup",
        "list_available_portfolios",
        "get_portfolio_data",
    }


async def test_credential_setup_does_not_require_credentials():
    async with create_connected_server_and_client_session(main.mcp, raise_exceptions=True) as session:
        result = await session.call_tool("credential_setup", {})

    assert result.isError is False
    assert result.structuredContent["status"] == "setup_required"
    assert len(result.structuredContent["modes"]) == 2


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
