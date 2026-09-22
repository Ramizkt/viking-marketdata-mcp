from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest
from mcp.server.auth.provider import AccessToken
from pydantic import ValidationError
from starlette.testclient import TestClient

from app import main
from app.config import Settings
from app.oauth import OAUTH_SCOPE

ORIGIN = "https://k1forge.com"
MCP_HEADERS = "authorization,content-type,mcp-protocol-version,mcp-session-id,last-event-id"


@pytest.mark.parametrize("method", ["GET", "POST", "DELETE", "OPTIONS"])
@pytest.mark.parametrize("path", ["/mcp", "/mcp/"])
def test_mcp_preflight_without_bearer_token(method, path):
    response = TestClient(main.app).options(
        path,
        headers={
            "Origin": ORIGIN,
            "Access-Control-Request-Method": method,
            "Access-Control-Request-Headers": MCP_HEADERS,
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ORIGIN
    assert "Origin" in response.headers["vary"]
    assert "access-control-allow-credentials" not in response.headers
    assert "www-authenticate" not in response.headers
    assert set(MCP_HEADERS.split(",")) <= {
        value.strip().lower() for value in response.headers["access-control-allow-headers"].split(",")
    }


@pytest.mark.parametrize("origin", ["https://other.example", "https://k1forge.com.evil.example", "null"])
def test_unlisted_origin_is_not_granted_mcp_browser_access(origin):
    client = TestClient(main.app)
    preflight = client.options(
        "/mcp", headers={"Origin": origin, "Access-Control-Request-Method": "POST"}
    )
    assert preflight.status_code == 400
    assert "access-control-allow-origin" not in preflight.headers
    actual = client.post("/mcp", headers={"Origin": origin}, json={})
    assert actual.status_code == 401
    assert "access-control-allow-origin" not in actual.headers


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer invalid-fixture-token"}])
def test_cors_does_not_bypass_mcp_oauth(headers):
    response = TestClient(main.app).post("/mcp", headers={"Origin": ORIGIN, **headers}, json={})
    assert response.status_code == 401
    assert response.headers["access-control-allow-origin"] == ORIGIN
    assert "resource_metadata=" in response.headers["www-authenticate"]
    assert "mcp-session-id" in response.headers["access-control-expose-headers"].lower()
    assert "access-control-allow-credentials" not in response.headers


def test_native_client_auth_and_sdk_oauth_cors_are_unchanged():
    client = TestClient(main.app)
    native = client.post("/mcp", json={})
    assert native.status_code == 401
    assert "access-control-allow-origin" not in native.headers
    # The SDK already permits browser OAuth clients; do not narrow that policy.
    registration = client.options(
        "/register",
        headers={
            "Origin": "https://oauth-client.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert registration.status_code == 200
    assert registration.headers["access-control-allow-origin"] == "*"


@pytest.mark.parametrize(
    "method, headers", [("PATCH", "content-type"), ("POST", "x-unapproved-header")]
)
def test_mcp_preflight_rejects_unapproved_method_or_header(method, headers):
    response = TestClient(main.app).options(
        "/mcp",
        headers={
            "Origin": ORIGIN,
            "Access-Control-Request-Method": method,
            "Access-Control-Request-Headers": headers,
        },
    )
    assert response.status_code == 400


def test_authenticated_mcp_and_lifespan_survive_cors(monkeypatch, tmp_path):
    # Each MCP session manager can run once. Load the actual app separately so the
    # existing full OAuth/PKCE regression still exercises its own unchanged app.
    config = Settings(
        export_dir=tmp_path / "exports", oauth_client_store_path=tmp_path / "oauth-clients.json"
    )
    spec = importlib.util.spec_from_file_location("app._cors_test_main", Path(main.__file__))
    module = importlib.util.module_from_spec(spec)
    with patch("app.config.get_settings", return_value=config):
        spec.loader.exec_module(module)
    start = Mock()
    close = AsyncMock()
    monkeypatch.setattr(module.viking_clients, "start", start)
    monkeypatch.setattr(module.viking_clients, "close", close)
    verify = AsyncMock(
        return_value=AccessToken(token="fixture-token", client_id="fixture", scopes=[OAUTH_SCOPE])
    )
    monkeypatch.setattr(module.oauth_provider, "load_access_token", verify)
    with TestClient(module.app) as client:
        response = client.post(
            "/mcp",
            headers={
                "Origin": ORIGIN,
                "Authorization": "Bearer fixture-token",
                "Accept": "application/json, text/event-stream",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "CORS regression", "version": "1"},
                },
            },
        )
        assert response.status_code == 200
        assert response.json()["result"]["serverInfo"]["name"] == "Viking Market Data"
        assert response.headers["access-control-allow-origin"] == ORIGIN
        verify.assert_awaited_once_with("fixture-token")
        start.assert_called_once()
    close.assert_awaited_once()


@pytest.mark.parametrize(
    "origin",
    ["*", "https://*.example.com", "null", "ftp://example.com", "https://user@example.com",
     "https://example.com/", "https://example.com/path", "https://example.com?q=1",
     "https://example.com#fragment", " https://example.com", "https://example.com:70000"],
)
def test_cors_configuration_rejects_non_origins(origin):
    with pytest.raises(ValidationError):
        Settings(cors_allowed_origins=[origin])


def test_cors_origins_are_configurable_and_can_be_disabled(monkeypatch):
    monkeypatch.delenv("CORS_ALLOWED_ORIGINS", raising=False)
    assert Settings(_env_file=None).cors_allowed_origins == [ORIGIN]
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", '["https://k1forge.com","http://localhost:5173"]')
    assert Settings(_env_file=None).cors_allowed_origins == [ORIGIN, "http://localhost:5173"]
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "[]")
    assert Settings(_env_file=None).cors_allowed_origins == []
