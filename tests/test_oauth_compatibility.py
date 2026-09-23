from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import parse_qs, urlparse

import pytest
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.shared.memory import create_connected_server_and_client_session
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.routing import Mount, Route
from starlette.testclient import TestClient

from app.auth_compat import (
    CompatibleFastMCP, OAuthCompatibilityMiddleware, WRITE_TOOL_NAMES,
    metadata_routes, offered_scopes, register_auth_status,
)
from app.config import Settings
from app.credentials import VikingCredentials
from app.oauth import OAUTH_SCOPE, PORTFOLIO_WRITE_SCOPE, TOKEN_AAD, VikingOAuthProvider
from app.portfolio_tools import register_portfolio_control_tools
from app.viking_client import VikingClient

BASE = "http://127.0.0.1:8000"
RESOURCE = BASE + "/mcp"
BOTH = [OAUTH_SCOPE, PORTFOLIO_WRITE_SCOPE]
VERIFIER = "fixture-verifier-" * 4
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).decode().rstrip("=")
FORM = {"email": "fixture@example.invalid", "api_key": "fixture-only", "role": "trader", "mode": "session"}
ARGS = {"robot_id": "fixture-robot", "portfolio": "fixture-portfolio", "side": "sell"}


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    settings = Settings(
        public_base_url=BASE, export_signing_key="fixture-only-key",
        oauth_client_store_path=tmp_path / "oauth-clients.json", viking_portfolio_writes_enabled=True,
    )
    provider = VikingOAuthProvider(settings)
    authenticate = AsyncMock()
    monkeypatch.setattr(VikingClient, "authenticate", authenticate)
    mcp = CompatibleFastMCP(
        "fixture-mcp", auth_server_provider=provider,
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(BASE), resource_server_url=AnyHttpUrl(RESOURCE),
            required_scopes=[OAUTH_SCOPE],
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=BOTH, default_scopes=offered_scopes(settings),
            ),
        ), stateless_http=True, json_response=True, streamable_http_path="/mcp",
    )
    viking = SimpleNamespace(execute_portfolio_control=AsyncMock(return_value={"r": "p", "data": {}}))
    factory = Mock(return_value=SimpleNamespace(client=viking))
    register_portfolio_control_tools(mcp, settings=settings, service_factory=factory)
    register_auth_status(mcp, settings)

    @mcp.tool()
    async def fixture_read() -> dict:
        return {"read": "unchanged"}

    sdk_app = mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(_):
        async with mcp.session_manager.run():
            yield
        await provider.close()

    root = Starlette(routes=[
        *metadata_routes(settings),
        Route("/oauth/connect/{pending_id:str}", provider.connect_page, methods=["GET", "POST"]),
        Mount("/", app=sdk_app),
    ], lifespan=lifespan)
    app = CORSMiddleware(
        OAuthCompatibilityMiddleware(root, settings=settings, provider=provider),
        allow_origins=["https://k1forge.com"], allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "MCP-Protocol-Version"],
        expose_headers=["WWW-Authenticate"],
    )
    with TestClient(app, base_url=BASE) as http:
        yield SimpleNamespace(http=http, settings=settings, provider=provider, factory=factory,
                              viking=viking, authenticate=authenticate, mcp=mcp)


def register(rt, scope=None, confidential=False, callback=None):
    body = {
        "client_name": "fixture-client", "redirect_uris": [callback or "http://127.0.0.1:9999/callback"],
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
        "token_endpoint_auth_method": "client_secret_post" if confidential else "none",
    }
    if scope is not None:
        body["scope"] = scope
    response = rt.http.post("/register", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def start(rt, client, scope=None, resource=RESOURCE, verifier_challenge=CHALLENGE):
    params = {
        "response_type": "code", "client_id": client["client_id"],
        "redirect_uri": client["redirect_uris"][0], "state": "fixture-state",
        "code_challenge": verifier_challenge, "code_challenge_method": "S256",
    }
    if scope is not None:
        params["scope"] = scope
    if resource is not None:
        params["resource"] = resource
    return rt.http.get("/authorize", params=params, follow_redirects=False)


def finish(rt, client, connect_url, form, verifier=VERIFIER):
    response = rt.http.post(connect_url, data=form, follow_redirects=False)
    assert response.status_code == 302, response.text
    query = parse_qs(urlparse(response.headers["location"]).query)
    assert query["state"] == ["fixture-state"]
    data = {
        "grant_type": "authorization_code", "client_id": client["client_id"], "code": query["code"][0],
        "redirect_uri": client["redirect_uris"][0], "code_verifier": verifier,
    }
    if client.get("client_secret"):
        data["client_secret"] = client["client_secret"]
    return rt.http.post("/token", data=data)


def login(rt, client, *, access="read", mode="session", scope=None, legacy=False, resource=RESOURCE, email=None):
    form = {**FORM, "mode": mode}
    if not legacy:
        form["access_mode"] = access
    if access == "write":
        form["allow_portfolio_writes"] = "yes"
    if email:
        form["email"] = email
    response = start(rt, client, scope=scope, resource=resource)
    assert response.status_code == 302, response.text
    result = finish(rt, client, response.headers["location"], form)
    assert result.status_code == 200, result.text
    return result.json()


def rpc(rt, token, method, params=None):
    headers = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-11-25"}
    if token:
        headers["Authorization"] = "Bearer " + token
    return rt.http.post("/mcp", json={"jsonrpc": "2.0", "id": 7, "method": method, "params": params or {}}, headers=headers)


def refresh(rt, client, token):
    data = {"grant_type": "refresh_token", "client_id": client["client_id"], "refresh_token": token["refresh_token"]}
    if client.get("client_secret"):
        data["client_secret"] = client["client_secret"]
    return rt.http.post("/token", data=data)


def test_discovery_driven_client_gets_optional_write_choice(runtime):
    rt = runtime
    denied = rpc(rt, None, "initialize")
    assert denied.status_code == 401
    header = denied.headers["www-authenticate"]
    assert "resource_metadata=" in header and 'scope="' not in header
    metadata = rt.http.get("/.well-known/oauth-protected-resource/mcp").json()
    assert metadata["scopes_supported"] == BOTH
    assert metadata["resource"] == RESOURCE
    assert rt.http.get("/.well-known/oauth-protected-resource").json() == metadata
    auth_meta = rt.http.get("/.well-known/oauth-authorization-server").json()
    assert "none" in auth_meta["token_endpoint_auth_methods_supported"]
    assert auth_meta["code_challenge_methods_supported"] == ["S256"]
    client = register(rt)
    assert client["scope"].split() == BOTH
    response = start(rt, client, " ".join(metadata["scopes_supported"]))
    page = rt.http.get(response.headers["location"])
    assert 'name="access_mode" value="read" checked' in page.text
    assert 'name="allow_portfolio_writes"' in page.text
    assert 'value="yes" checked' not in page.text
    assert "127.0.0.1:9999" in page.text
    rt.authenticate.assert_not_awaited()


@pytest.mark.parametrize("access", ["read", "write"])
@pytest.mark.parametrize("mode", ["session", "local"])
def test_complete_oauth_and_runtime_permissions(runtime, access, mode):
    rt = runtime
    client = register(rt)
    token = login(rt, client, access=access, mode=mode)
    assert token["scope"].split() == (BOTH if access == "write" else [OAUTH_SCOPE])
    status = rpc(rt, token["access_token"], "tools/call", {"name": "get_authorization_status"})
    assert status.status_code == 200, status.text
    value = status.json()["result"]["structuredContent"]
    assert value["can_write"] is (access == "write")
    assert value["viking_role_permissions_verified"] is False
    assert token["access_token"] not in status.text and FORM["api_key"] not in status.text
    assert FORM["email"] not in status.text
    result = rpc(rt, token["access_token"], "tools/call", {
        "name": "stop_portfolio_trading", "arguments": {**ARGS, "dry_run": False, "confirm": True},
    })
    if access == "read":
        assert result.status_code == 403, result.text
        assert 'error="insufficient_scope"' in result.headers["www-authenticate"]
        assert 'scope="viking.read viking.portfolio.write"' in result.headers["www-authenticate"]
        assert result.json()["result"]["_meta"]["mcp/www_authenticate"]
        assert result.json()["result"]["structuredContent"]["operation_sent"] is False
        rt.factory.assert_not_called()
        rt.viking.execute_portfolio_control.assert_not_awaited()
    else:
        assert result.status_code == 200, result.text
        assert result.json()["result"]["structuredContent"]["status"] == "accepted"
        assert rt.viking.execute_portfolio_control.await_count == 1
    unchanged = rpc(rt, token["access_token"], "tools/call", {"name": "fixture_read"})
    assert unchanged.status_code == 200
    assert unchanged.json()["result"]["structuredContent"] == {"read": "unchanged"}


@pytest.mark.parametrize("write", [False, True])
def test_unchanged_lovable_programmatic_flow(runtime, write):
    rt = runtime
    scope = " ".join(BOTH) if write else OAUTH_SCOPE
    client = register(rt, scope, confidential=True, callback=BASE + "/oauth/noop-callback")
    token = login(rt, client, access="write" if write else "read", scope=scope, legacy=True, resource=None)
    assert token["scope"] == scope
    renewed = refresh(rt, client, token)
    assert renewed.status_code == 200, renewed.text
    assert renewed.json()["scope"] == scope
    assert renewed.json()["refresh_token"] != token["refresh_token"]
    assert refresh(rt, client, token).status_code == 400


def test_read_only_scope_cannot_be_expanded_by_form(runtime):
    rt = runtime
    client = register(rt, OAUTH_SCOPE)
    response = start(rt, client, OAUTH_SCOPE)
    url = response.headers["location"]
    page = rt.http.get(url)
    assert 'name="allow_portfolio_writes"' not in page.text
    refused = rt.http.post(url, data={**FORM, "access_mode": "write", "allow_portfolio_writes": "yes"}, follow_redirects=False)
    assert refused.status_code == 200 and not rt.provider._codes
    rt.authenticate.assert_not_awaited()
    token = login(rt, client, access="write", scope=OAUTH_SCOPE, legacy=True)
    assert token["scope"] == OAUTH_SCOPE  # legacy forged checkbox does not escalate
    invalid = start(rt, client, " ".join(BOTH))
    assert "invalid_scope" in invalid.headers["location"]


def test_missing_consent_can_continue_read_only(runtime):
    rt = runtime
    client = register(rt)
    url = start(rt, client).headers["location"]
    refused = rt.http.post(url, data={**FORM, "access_mode": "write"}, follow_redirects=False)
    assert refused.status_code == 200 and not rt.provider._codes
    rt.authenticate.assert_not_awaited()
    token = finish(rt, client, url, {**FORM, "access_mode": "read"})
    assert token.status_code == 200 and token.json()["scope"] == OAUTH_SCOPE


def test_preview_and_invalid_booleans_do_not_trigger_step_up(runtime):
    rt = runtime
    token = login(rt, register(rt))["access_token"]
    result = rpc(rt, token, "tools/call", {"name": "stop_portfolio_trading", "arguments": {**ARGS, "dry_run": True}})
    assert result.status_code == 200 and result.json()["result"]["structuredContent"]["status"] == "preview"
    for args in ({"dry_run": "false", "confirm": True}, {"dry_run": False, "confirm": False}):
        result = rpc(rt, token, "tools/call", {"name": "stop_portfolio_trading", "arguments": {**ARGS, **args}})
        assert result.status_code == 200 and result.json()["result"]["isError"]
        assert "www-authenticate" not in result.headers
    rt.viking.execute_portfolio_control.assert_not_awaited()


def test_tool_metadata_and_diagnostic_do_not_contact_viking(runtime):
    rt = runtime
    token = login(rt, register(rt))["access_token"]
    tools = rpc(rt, token, "tools/list").json()["result"]["tools"]
    for tool in tools:
        expected = BOTH if tool["name"] in WRITE_TOOL_NAMES else [OAUTH_SCOPE]
        assert tool["securitySchemes"] == [{"type": "oauth2", "scopes": expected}]
        assert tool["_meta"]["securitySchemes"] == tool["securitySchemes"]
    rt.factory.assert_not_called()


def test_invalid_token_and_wrong_resource(runtime):
    rt = runtime
    result = rpc(rt, "invalid-fixture-token", "tools/list")
    assert result.status_code == 401 and 'error="invalid_token"' in result.headers["www-authenticate"]
    client = register(rt)
    wrong = start(rt, client, resource="https://wrong.example.invalid/mcp")
    assert "error=" in wrong.headers["location"] and not rt.provider._pending
    rt.authenticate.assert_not_awaited()


def test_pkce_verifier_is_still_enforced(runtime):
    rt = runtime
    client = register(rt)
    url = start(rt, client).headers["location"]
    result = finish(rt, client, url, {**FORM, "access_mode": "read"}, verifier="wrong-verifier" * 5)
    assert result.status_code == 400 and "invalid_grant" in result.text


@pytest.mark.parametrize("mode", ["session", "local"])
def test_downgrade_revokes_old_write_only_for_same_connection(runtime, mode):
    rt = runtime
    client = register(rt)
    old = login(rt, client, access="write", mode=mode)
    other_client = login(rt, register(rt), access="write", mode=mode)
    other_user = login(rt, client, access="write", mode=mode, email="other@example.invalid")
    login(rt, client, access="read", mode=mode)
    def can_write(token):
        response = rpc(rt, token["access_token"], "tools/call", {"name": "get_authorization_status"})
        return response.json()["result"]["structuredContent"]["can_write"]
    assert not can_write(old)
    assert can_write(other_client) and can_write(other_user)
    if mode == "session":
        renewed = refresh(rt, client, old)
        assert renewed.status_code == 200 and renewed.json()["scope"] == OAUTH_SCOPE
    new_write = login(rt, client, access="write", mode=mode)
    assert can_write(new_write) and not can_write(old)


async def test_legacy_remembered_token_survives_upgrade_but_not_explicit_downgrade(tmp_path):
    settings = Settings(public_base_url=BASE, export_signing_key="fixture-key", oauth_client_store_path=tmp_path / "clients.json")
    provider = VikingOAuthProvider(settings)
    credentials = VikingCredentials(**{"email": FORM["email"], "api_key": FORM["api_key"], "role": "trader"})
    payload = {
        "kind": "access", "email": credentials.email, "api_key": credentials.api_key, "role": credentials.role,
        "client_id": "legacy-fixture", "scopes": BOTH, "resource": None,
        "sub": provider._subject(credentials), "exp": int(time.time()) + 300,
    }
    nonce = secrets.token_bytes(12)
    token = "v1_" + base64.urlsafe_b64encode(nonce + provider._cipher.encrypt(nonce, json.dumps(payload).encode(), TOKEN_AAD)).decode().rstrip("=")
    assert (await provider.load_access_token(token)).scopes == BOTH
    provider._grants.downgrade(payload["client_id"], payload["sub"])
    restarted = VikingOAuthProvider(settings)
    assert (await restarted.load_access_token(token)).scopes == [OAUTH_SCOPE]
    assert oct(provider._grants.path.stat().st_mode & 0o777) == "0o600"
    raw = provider._grants.path.read_bytes()
    for secret in (credentials.email, credentials.api_key, token, payload["client_id"]):
        assert secret.encode() not in raw


async def test_direct_tool_error_has_chatgpt_metadata(runtime, monkeypatch):
    import app.portfolio_tools as tools
    rt = runtime
    monkeypatch.setattr(tools, "get_access_token", lambda: SimpleNamespace(scopes=[OAUTH_SCOPE]))
    async with create_connected_server_and_client_session(rt.mcp, raise_exceptions=True) as session:
        result = await session.call_tool("stop_portfolio_trading", {**ARGS, "dry_run": False, "confirm": True})
    assert result.isError
    assert result.meta["mcp/www_authenticate"]
    rt.factory.assert_not_called()


def test_cors_exposes_step_up_challenge_without_cookies(runtime):
    rt = runtime
    token = login(rt, register(rt))["access_token"]
    result = rt.http.post("/mcp", json={"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {
        "name": "stop_portfolio_trading", "arguments": {**ARGS, "dry_run": False, "confirm": True},
    }}, headers={"Origin": "https://k1forge.com", "Authorization": "Bearer " + token,
                "Accept": "application/json, text/event-stream"})
    assert result.status_code == 403
    assert "WWW-Authenticate" in result.headers["access-control-expose-headers"]
    assert "access-control-allow-credentials" not in result.headers


def test_server_disabled_is_not_a_reauthorization_loop(runtime):
    rt = runtime
    token = login(rt, register(rt))["access_token"]
    rt.settings.viking_portfolio_writes_enabled = False
    result = rpc(rt, token, "tools/call", {"name": "stop_portfolio_trading", "arguments": {**ARGS, "dry_run": False, "confirm": True}})
    assert result.status_code == 200 and result.json()["result"]["isError"]
    assert "administrator" in result.text
    assert "www-authenticate" not in result.headers
    rt.factory.assert_not_called()
