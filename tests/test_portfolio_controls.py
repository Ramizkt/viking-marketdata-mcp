from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from app import main, portfolio_tools
from app.config import Settings
from app.credentials import VikingCredentials
from app.oauth import OAUTH_SCOPE, PORTFOLIO_WRITE_SCOPE
from app.portfolio_control import (
    PortfolioControlService, PortfolioTarget, WriteNotSent, WriteOutcomeUnknown,
    command_for, normalize_user_fields, validate_user_field_template,
)
from app.portfolio_tools import WRITE_TOOL_NAMES
from app.viking_client import VikingAPIError, VikingClient, VikingProtocolError

TARGET = {"robot_id": "test-robot", "portfolio": "test-portfolio"}


def acknowledgement(kind="portfolio.update", portfolio="test-portfolio"):
    return {"type": kind, "eid": "test-eid", "ts": 1, "r": "p",
            "data": {"r_id": "test-robot", "p_id": portfolio}}


def template():
    fields = {"portfolio": [
        {"field": f"uf{i}", "editor": "user_value", "disabled": False,
         "min": -100, "max": 100, "max_len": 8}
        for i in range(20)
    ]}
    return {"template_id": "test-template",
            "template": {"template_id": "test-template", "template_fields": fields},
            "template_fields": fields}


@pytest.fixture
def client(tmp_path):
    c = VikingClient(Settings(export_dir=tmp_path),
                     VikingCredentials(email="fixture@example.invalid", api_key="fixture-only", role="trader"))
    c._ensure_connected = AsyncMock()
    c._request_connected = AsyncMock(return_value=acknowledgement())
    c.request = AsyncMock(side_effect=AssertionError("Retrying read transport must not handle writes"))
    c.close = AsyncMock()
    c.get_portfolio_template = AsyncMock(return_value=template())
    return c


@pytest.mark.parametrize("index", range(20))
def test_all_user_fields(index):
    assert normalize_user_fields({f"uf{index}": 2}) == {f"uf{index}": {"v": 2}}


@pytest.mark.parametrize("fields", [
    {}, {"uf20": 1}, {"uf-1": 1}, {"uf01": 1}, {"uf0\n": 1}, {"re_buy": False},
    {"uf0": True}, {"uf0": "12"}, {"uf0": None}, {"uf0": []},
    {"uf0": float("nan")}, {"uf0": float("inf")}, {"uf0": 2**53},
    {"uf0": {}}, {"uf0": {"v": None}}, {"uf0": {"c": None}},
    {"uf0": {"c": 123}}, {"uf0": {"x": 1}}, {"uf0": {"c": "\ud800"}},
])
def test_invalid_user_fields(fields):
    with pytest.raises((ValueError, TypeError)):
        normalize_user_fields(fields)


@pytest.mark.parametrize("value, expected", [
    (0, {"v": 0}), (1.5, {"v": 1.5}), ({"c": ""}, {"c": ""}),
    ({"v": 0, "c": "test"}, {"v": 0, "c": "test"}),
])
def test_partial_user_field_payload(value, expected):
    kind, data = command_for(PortfolioTarget(**TARGET), "user_fields", {"uf0": value})
    assert kind == "portfolio.update"
    assert data == {"r_id": TARGET["robot_id"], "portfolio": {"name": TARGET["portfolio"], "uf0": expected}}


@pytest.mark.parametrize("side, flags", [
    ("both", {"re_sell": False, "re_buy": False}),
    ("sell", {"re_sell": False}), ("buy", {"re_buy": False}),
])
def test_stop_only_changes_requested_flags(side, flags):
    kind, data = command_for(PortfolioTarget(**TARGET), "stop", side=side)
    assert kind == "portfolio.update"
    assert data == {"r_id": TARGET["robot_id"], "portfolio": {"name": TARGET["portfolio"], **flags}}


@pytest.mark.parametrize("action, kind", [
    ("hard_stop", "portfolio.hard_stop"), ("stop_formulas", "portfolio.formulas_stop"),
])
async def test_special_commands_use_exact_api_types(client, action, kind):
    client._request_connected.return_value = acknowledgement(kind)
    await client.execute_portfolio_control(**TARGET, action=action)
    assert client._request_connected.await_args.args == (
        kind, {"r_id": TARGET["robot_id"], "p_id": TARGET["portfolio"]}
    )
    client.request.assert_not_called()


def test_template_limits_are_authoritative():
    resolved = template()
    validate_user_field_template({"uf0": {"v": 100, "c": "я" * 4}}, resolved)
    for patch in ({"v": 101}, {"c": "я" * 5}):
        with pytest.raises(ValueError):
            validate_user_field_template({"uf0": patch}, resolved)
    resolved["template_fields"]["portfolio"][0]["disabled"] = True
    with pytest.raises(ValueError):
        validate_user_field_template({"uf0": {"v": 1}}, resolved)


@pytest.mark.parametrize("damage", ["id", "fields", "missing", "duplicate", "bounds", "caption"])
def test_malformed_templates_fail_closed(damage):
    resolved = template()
    entries = resolved["template_fields"]["portfolio"]
    if damage == "id":
        resolved["template"]["template_id"] = "different"
    elif damage == "fields":
        resolved["template_fields"]["portfolio"] = {}
    elif damage == "missing":
        entries.pop(0)
    elif damage == "duplicate":
        entries.append(entries[0].copy())
    elif damage == "bounds":
        entries[0]["min"] = float("nan")
    else:
        entries[0]["max_len"] = True
    with pytest.raises(VikingProtocolError):
        validate_user_field_template({"uf0": {"v": 1, "c": "x"}}, resolved)


async def test_preview_does_not_send(client):
    result = await PortfolioControlService(client).run(targets=[TARGET], action="user_fields", fields={"uf0": 1})
    assert result["status"] == "preview"
    assert result["items"][0]["request"]["data"]["portfolio"] == {"name": TARGET["portfolio"], "uf0": {"v": 1}}
    client.get_portfolio_template.assert_awaited_once_with(**TARGET)
    client._request_connected.assert_not_awaited()


@pytest.mark.parametrize("kwargs", [
    {"targets": []}, {"targets": [TARGET, TARGET]},
    {"targets": [TARGET, {"robot_id": "x", "portfolio": "*"}]},
    {"targets": [TARGET, {"robot_id": 1, "portfolio": "x"}]},
    {"targets": [TARGET], "dry_run": False},
    {"targets": [TARGET], "dry_run": "false", "confirm": True},
])
async def test_batch_validation_precedes_all_writes(client, kwargs):
    with pytest.raises(ValueError):
        await PortfolioControlService(client).run(action="stop", **kwargs)
    client._request_connected.assert_not_awaited()


@pytest.mark.parametrize("error", [TimeoutError("fixture"), ConnectionError("fixture")])
async def test_transport_failure_never_replays(client, error):
    client._request_connected.side_effect = error
    with pytest.raises(WriteOutcomeUnknown):
        await client.execute_portfolio_control(**TARGET, action="stop")
    assert client._request_connected.await_count == 1
    client.request.assert_not_called()
    client.close.assert_awaited_once()


async def test_connect_failure_is_not_sent(client):
    client._ensure_connected.side_effect = ConnectionError("fixture")
    with pytest.raises(WriteNotSent):
        await client.execute_portfolio_control(**TARGET, action="stop")
    client._request_connected.assert_not_awaited()


@pytest.mark.parametrize("mutation", [
    {"type": "portfolio.hasrd_stop"}, {"r": "s"}, {"ts": None},
    {"data": {"r_id": "other", "p_id": TARGET["portfolio"]}},
    {"data": {"r_id": TARGET["robot_id"], "p_id": "other"}},
])
async def test_malformed_ack_is_unknown_not_success(client, mutation):
    client._request_connected.return_value = {**acknowledgement(), **mutation}
    with pytest.raises(WriteOutcomeUnknown):
        await client.execute_portfolio_control(**TARGET, action="stop")
    assert client._request_connected.await_count == 1


async def test_batch_reports_rejection_and_unknown_and_remaining(client):
    error = VikingAPIError("fixture rejection", code="denied", response={"r": "e", "data": {}})
    client.execute_portfolio_control = AsyncMock(side_effect=[
        acknowledgement(), error, WriteOutcomeUnknown(TimeoutError("fixture")),
    ])
    targets = [{"robot_id": "test-robot", "portfolio": f"test-{i}"} for i in range(4)]
    result = await PortfolioControlService(client).run(
        targets=targets, action="hard_stop", dry_run=False, confirm=True
    )
    assert [i["status"] for i in result["items"]] == ["accepted", "rejected", "outcome_unknown", "not_sent"]
    assert result["status"] == "partial_failure"
    assert result["has_errors"] is True
    assert result["atomic"] is False
    assert result["automatic_retry"] is False
    assert all(i["verified"] is False for i in result["items"])
    assert result["items"][1]["error"]["code"] == "denied"
    assert client.execute_portfolio_control.await_count == 3


async def test_write_tool_annotations_and_read_tools_unchanged():
    async with create_connected_server_and_client_session(main.mcp, raise_exceptions=True) as session:
        tools = {t.name: t for t in (await session.list_tools()).tools}
    assert len(tools) == 55
    for name, tool in tools.items():
        assert tool.annotations.readOnlyHint is (name not in WRITE_TOOL_NAMES)
        if name in WRITE_TOOL_NAMES:
            assert tool.annotations.destructiveHint is True
            assert tool.annotations.idempotentHint is False
            assert tool.inputSchema["properties"]["dry_run"]["default"] is True
            assert tool.inputSchema["properties"]["confirm"]["default"] is False


@pytest.mark.parametrize("enabled, scopes, confirmation, expected", [
    (False, [OAUTH_SCOPE, PORTFOLIO_WRITE_SCOPE], True, "error"),
    (True, [OAUTH_SCOPE], True, "error"),
    (True, [OAUTH_SCOPE, PORTFOLIO_WRITE_SCOPE], False, "error"),
    (True, [OAUTH_SCOPE, PORTFOLIO_WRITE_SCOPE], True, "accepted"),
])
async def test_mcp_requires_flag_scope_and_confirmation(client, monkeypatch, enabled, scopes, confirmation, expected):
    monkeypatch.setattr(main.settings, "viking_portfolio_writes_enabled", enabled)
    monkeypatch.setattr(portfolio_tools, "get_access_token", lambda: SimpleNamespace(scopes=scopes))
    factory = Mock(return_value=SimpleNamespace(client=client))
    monkeypatch.setattr(main, "_service_for_request", factory)
    async with create_connected_server_and_client_session(main.mcp, raise_exceptions=True) as session:
        result = await session.call_tool("stop_portfolios", {
            "targets": [TARGET], "dry_run": False, "confirm": confirmation,
        })
    assert result.structuredContent["status"] == expected
    assert result.isError is (expected == "error")
    if expected == "error":
        factory.assert_not_called()
        client._request_connected.assert_not_awaited()
    else:
        client._request_connected.assert_awaited_once()


@pytest.mark.parametrize("wrong", ["false", 0, 1, None])
async def test_mcp_rejects_coerced_confirmation(client, monkeypatch, wrong):
    monkeypatch.setattr(main, "_service_for_request", lambda: SimpleNamespace(client=client))
    async with create_connected_server_and_client_session(main.mcp, raise_exceptions=True) as session:
        result = await session.call_tool("stop_portfolios", {"targets": [TARGET], "confirm": wrong})
    assert result.isError is True
    client._request_connected.assert_not_awaited()
