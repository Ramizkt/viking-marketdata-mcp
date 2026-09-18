"""Temporary reviewed refinements; removed with the integration workflow."""
import runpy
from pathlib import Path

runpy.run_path("scripts/integrate_portfolio_controls.py", run_name="__main__")

p = Path("app/oauth.py")
s = p.read_text()
s = s.replace('''        if PORTFOLIO_WRITE_SCOPE in (pending.params.scopes or []):
            if not self.settings.viking_portfolio_writes_enabled or form.get("allow_portfolio_writes") != "yes":
                return self._render_page(
                    pending_id, selected_mode=mode, email=email, role=role,
                    error="Для изменения полей и остановок нужно отдельное явное разрешение на операции записи.",
                )''', '''        if PORTFOLIO_WRITE_SCOPE in (pending.params.scopes or []) and (
            not self.settings.viking_portfolio_writes_enabled
            or form.get("allow_portfolio_writes") != "yes"
        ):
            return self._render_page(
                pending_id, selected_mode=mode, email=email, role=role,
                error="Нужно отдельное явное разрешение на изменение полей и остановки портфелей.",
            )''')
p.write_text(s)

p = Path("app/portfolio_control.py")
s = p.read_text().replace("import asyncio\n", "import asyncio\nimport contextlib\n")
s = s.replace('''            await client.close()
            raise''', '''            with contextlib.suppress(Exception):
                await client.close()
            raise''')
replacements = {
    'Read back the affected fields with get_current_portfolio_data; a write acknowledgement is not readback.':
        'Read back fields with get_current_portfolio_data; acknowledgement is not readback.',
    'Formulas remain active and may re-enable trading. Use Stop formulas only on explicit user instruction.':
        'Formulas can re-enable trading. Stop formulas requires a separate explicit user instruction.',
    'Stop formulas also disables formula calculations and changes formula-driven modes to constants/Standard.':
        'Stop formulas disables formulas and changes formula-driven modes to constants/Standard.',
    'Accepted means Viking acknowledged the command, not verified trading state or exchange cancellation.':
        'Accepted means API acknowledgement, not verified trading state or exchange cancellation.',
    'Use get_robot_portfolio_trading_status for trading state and order subscriptions for active orders.':
        'Verify trading via get_robot_portfolio_trading_status and active orders via subscriptions.',
    'Preview is not a reservation: permissions, templates and portfolio state can change before execution.':
        'Preview is not a reservation: permissions, templates and state may change before execution.',
    'Stop operations do not close positions. No automatic rollback or replay is performed.':
        'Stops do not send position-closing commands. No automatic rollback or replay is performed.',
}
for old, new in replacements.items():
    s = s.replace(old, new)
p.write_text(s)

p = Path("app/portfolio_tools.py")
s = p.read_text().replace(
    'and explicitly consent in the browser. Existing grants are not upgraded automatically.',
    'and consent in the browser. Old grants are not upgraded automatically.'
)
p.write_text(s)

p = Path("README.md")
s = p.read_text().replace("вне текущего read-only MCP", "вне текущего MCP")
s = s.replace("- сервер предоставляет только read-only инструменты.",
              "- чтение доступно по viking.read; пять операций записи требуют отдельного разрешения.")
s = s.replace("https://fkviking.github.io/bot-doc/docs/interface.html", "https://fkviking.github.io/bot-doc/docs/getting-started.html")
p.write_text(s)

p = Path("AGENTS.md")
s = p.read_text().replace("аргументы, read-only annotations, понятное описание", "аргументы, корректные annotations, понятное описание")
s = s.replace("- внешний MCP tool schema и read-only annotation.",
              "- внешний MCP tool schema и корректные read/write annotations.")
p.write_text(s)

p = Path("tests/test_mcp.py")
s = p.read_text().replace("from app import main\n", "from app import main\nfrom app.portfolio_tools import WRITE_TOOL_NAMES\n")
s = s.replace(
    "tool.annotations is not None and tool.annotations.readOnlyHint is True\n        for tool in tools.values()",
    "tool.annotations is not None\n        and tool.annotations.readOnlyHint is (tool.name not in WRITE_TOOL_NAMES)\n        for tool in tools.values()",
)
p.write_text(s)

p = Path("tests/test_portfolio_controls.py")
s = p.read_text().replace("from types import SimpleNamespace", "import asyncio\n\nfrom types import SimpleNamespace")
if "test_mcp_field_patch_survives_schema_validation" not in s:
    s += '''

async def test_mcp_field_patch_survives_schema_validation(client, monkeypatch):
    monkeypatch.setattr(main.settings, "viking_portfolio_writes_enabled", False)
    monkeypatch.setattr(main, "_service_for_request", lambda: SimpleNamespace(client=client))
    async with create_connected_server_and_client_session(main.mcp, raise_exceptions=True) as session:
        result = await session.call_tool("update_portfolio_user_fields", {
            **TARGET, "fields": {"uf0": 0, "uf1": {"c": ""}, "uf19": {"v": -1.5, "c": "test"}},
        })
    assert result.isError is False
    assert result.structuredContent["status"] == "preview"
    patch = result.structuredContent["items"][0]["request"]["data"]["portfolio"]
    assert patch == {"name": TARGET["portfolio"], "uf0": {"v": 0}, "uf1": {"c": ""},
                     "uf19": {"v": -1.5, "c": "test"}}
    client._request_connected.assert_not_awaited()


@pytest.mark.parametrize("fields", [{"uf0": True}, {"uf0": "12"}, {"uf0": {"v": False}}, {"uf20": 1}])
async def test_mcp_field_schema_does_not_coerce_invalid_values(client, monkeypatch, fields):
    monkeypatch.setattr(main, "_service_for_request", lambda: SimpleNamespace(client=client))
    async with create_connected_server_and_client_session(main.mcp, raise_exceptions=True) as session:
        result = await session.call_tool("update_portfolio_user_fields", {**TARGET, "fields": fields})
    assert result.isError is True
    client._request_connected.assert_not_awaited()


async def test_user_field_execution_sends_only_patch(client):
    result = await PortfolioControlService(client).run(
        targets=[TARGET], action="user_fields", fields={"uf0": {"c": "x"}}, dry_run=False, confirm=True
    )
    assert result["status"] == "accepted"
    assert client._request_connected.await_args.args == (
        "portfolio.update", {"r_id": TARGET["robot_id"],
                             "portfolio": {"name": TARGET["portfolio"], "uf0": {"c": "x"}}}
    )
    client.get_portfolio_template.assert_awaited_once_with(**TARGET)


async def test_cancellation_after_send_never_replays(client):
    client._request_connected.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await client.execute_portfolio_control(**TARGET, action="stop")
    assert client._request_connected.await_count == 1
    client.request.assert_not_called()
    client.close.assert_awaited_once()


async def test_cleanup_failure_does_not_hide_unknown_outcome(client):
    client._request_connected.side_effect = TimeoutError("fixture")
    client.close.side_effect = ConnectionError("fixture cleanup")
    with pytest.raises(WriteOutcomeUnknown):
        await client.execute_portfolio_control(**TARGET, action="stop")
    assert client._request_connected.await_count == 1


async def test_oversized_batch_rejected_before_any_write(client):
    targets = [{"robot_id": "test-robot", "portfolio": f"test-{i}"} for i in range(201)]
    with pytest.raises(ValueError):
        await PortfolioControlService(client).run(
            targets=targets, action="stop", dry_run=False, confirm=True
        )
    client._request_connected.assert_not_awaited()
'''
p.write_text(s)
print("Refinements applied; no live portfolio calls were made.")
