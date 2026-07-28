from __future__ import annotations

import base64
import hashlib
import secrets
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from app.config import Settings
from app.oauth import OAUTH_SCOPE, VikingOAuthProvider
from app.viking_client import VikingClient


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        public_base_url="http://127.0.0.1:8000",
        export_signing_key="test-only-server-secret",
        oauth_session_idle_ttl_seconds=60,
        oauth_session_max_ttl_seconds=300,
        oauth_persistent_token_ttl_seconds=3600,
        oauth_client_store_path=tmp_path / "oauth-clients.json",
    )


def _client() -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id="codex-test-client",
        client_secret="test-secret",
        redirect_uris=[AnyUrl("http://127.0.0.1:9876/callback")],
        token_endpoint_auth_method="client_secret_post",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope=OAUTH_SCOPE,
    )


def _params() -> AuthorizationParams:
    return AuthorizationParams(
        state="state-123",
        scopes=[OAUTH_SCOPE],
        code_challenge="challenge",
        redirect_uri=AnyUrl("http://127.0.0.1:9876/callback"),
        redirect_uri_provided_explicitly=True,
        resource="http://127.0.0.1:8000/mcp",
    )


async def _authorize_and_submit(
    provider: VikingOAuthProvider,
    *,
    mode: str,
    monkeypatch,
):
    async def authenticate(_: VikingClient) -> None:
        return None

    monkeypatch.setattr(VikingClient, "authenticate", authenticate)
    client = _client()
    await provider.register_client(client)
    connect_url = await provider.authorize(client, _params())
    pending_id = connect_url.rsplit("/", 1)[-1]

    app = Starlette(
        routes=[
            Route(
                "/oauth/connect/{pending_id:str}",
                provider.connect_page,
                methods=["GET", "POST"],
            )
        ]
    )
    with TestClient(app, base_url="http://127.0.0.1:8000") as browser:
        page = browser.get(f"/oauth/connect/{pending_id}")
        assert page.status_code == 200
        assert "Только на эту сессию" in page.text
        assert "Запомнить на этом компьютере" in page.text
        assert "PowerShell" not in page.text
        assert "form-action" not in page.headers["content-security-policy"]

        response = browser.post(
            f"/oauth/connect/{pending_id}",
            data={
                "mode": mode,
                "email": "user@example.com",
                "api_key": "viking-secret",
                "role": "trader",
            },
            follow_redirects=False,
        )

    assert response.status_code == 302
    redirect = urlparse(response.headers["location"])
    query = parse_qs(redirect.query)
    assert query["state"] == ["state-123"]
    authorization_code = await provider.load_authorization_code(client, query["code"][0])
    assert authorization_code is not None
    return client, authorization_code


async def test_session_mode_keeps_credentials_only_in_memory(monkeypatch, tmp_path):
    provider = VikingOAuthProvider(_settings(tmp_path))
    client, authorization_code = await _authorize_and_submit(
        provider,
        mode="session",
        monkeypatch=monkeypatch,
    )

    token = await provider.exchange_authorization_code(client, authorization_code)
    assert token.access_token.startswith("s1_")
    assert token.refresh_token is not None
    assert token.refresh_token.startswith("sr1_")
    assert token.expires_in == provider.settings.oauth_session_idle_ttl_seconds
    assert "viking-secret" not in token.access_token
    assert "viking-secret" not in token.refresh_token
    assert provider.credentials_for_access_token(token.access_token).api_key == "viking-secret"

    stored = provider._session_tokens[token.access_token]
    stored.last_used -= provider.settings.oauth_session_idle_ttl_seconds + 1
    assert await provider.load_access_token(token.access_token) is None
    assert provider.credentials_for_access_token(token.access_token) is None


async def test_session_refresh_rotates_tokens_without_persisting_credentials(
    monkeypatch,
    tmp_path,
):
    provider = VikingOAuthProvider(_settings(tmp_path))
    client, authorization_code = await _authorize_and_submit(
        provider,
        mode="session",
        monkeypatch=monkeypatch,
    )
    original = await provider.exchange_authorization_code(client, authorization_code)

    refresh = await provider.load_refresh_token(client, original.refresh_token)
    assert refresh is not None
    refreshed = await provider.exchange_refresh_token(client, refresh, [OAUTH_SCOPE])

    assert refreshed.access_token != original.access_token
    assert refreshed.refresh_token != original.refresh_token
    assert await provider.load_refresh_token(client, original.refresh_token) is None
    assert await provider.load_access_token(refreshed.access_token) is not None
    assert provider.credentials_for_access_token(refreshed.access_token).api_key == "viking-secret"
    assert settings_file_contents(tmp_path) == ""


async def test_session_refresh_is_rejected_after_server_restart(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    provider = VikingOAuthProvider(settings)
    client, authorization_code = await _authorize_and_submit(
        provider,
        mode="session",
        monkeypatch=monkeypatch,
    )
    token = await provider.exchange_authorization_code(client, authorization_code)

    restarted = VikingOAuthProvider(settings)
    restored_client = await restarted.get_client(client.client_id)
    assert restored_client is not None
    assert await restarted.load_refresh_token(restored_client, token.refresh_token) is None


def settings_file_contents(tmp_path: Path) -> str:
    credential_files = [
        path
        for path in tmp_path.iterdir()
        if path.name != "oauth-clients.json"
    ]
    return "".join(path.read_text(encoding="utf-8") for path in credential_files)


async def test_local_mode_uses_self_contained_encrypted_token(monkeypatch, tmp_path):
    provider = VikingOAuthProvider(_settings(tmp_path))
    client, authorization_code = await _authorize_and_submit(
        provider,
        mode="local",
        monkeypatch=monkeypatch,
    )

    token = await provider.exchange_authorization_code(client, authorization_code)
    assert token.access_token.startswith("v1_")
    assert "user@example.com" not in token.access_token
    assert "viking-secret" not in token.access_token
    assert provider._session_tokens == {}

    recovered = provider.credentials_for_access_token(token.access_token)
    assert recovered is not None
    assert recovered.email == "user@example.com"
    assert recovered.api_key == "viking-secret"
    assert recovered.role == "trader"
    assert await provider.load_access_token(token.access_token) is not None


async def test_expired_local_token_is_rejected(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    provider = VikingOAuthProvider(settings)
    client, authorization_code = await _authorize_and_submit(
        provider,
        mode="local",
        monkeypatch=monkeypatch,
    )
    token = provider._encrypt_token(
        credentials=authorization_code.credentials,
        client_id=client.client_id,
        scopes=[OAUTH_SCOPE],
        resource=authorization_code.resource,
        subject="subject",
        expires_at=int(time.time()) - 1,
    )

    assert await provider.load_access_token(token) is None
    assert provider.credentials_for_access_token(token) is None


async def test_oauth_client_registration_survives_restart(tmp_path):
    settings = _settings(tmp_path)
    original = _client()
    first_provider = VikingOAuthProvider(settings)
    await first_provider.register_client(original)

    restarted_provider = VikingOAuthProvider(settings)
    restored = await restarted_provider.get_client(original.client_id)

    assert restored is not None
    assert restored.client_id == original.client_id
    assert restored.client_secret == original.client_secret
    assert restored.redirect_uris == original.redirect_uris
    assert settings.resolved_oauth_client_store_path.stat().st_mode & 0o777 == 0o600


async def test_legacy_client_registration_is_recovered_after_login(monkeypatch, tmp_path):
    async def authenticate(_: VikingClient) -> None:
        return None

    monkeypatch.setattr(VikingClient, "authenticate", authenticate)
    settings = _settings(tmp_path)
    provider = VikingOAuthProvider(settings)
    client_id = str(uuid4())
    recovered = await provider.get_client(client_id)
    assert recovered is not None

    connect_url = await provider.authorize(recovered, _params())
    pending_id = connect_url.rsplit("/", 1)[-1]
    app = Starlette(
        routes=[
            Route(
                "/oauth/connect/{pending_id:str}",
                provider.connect_page,
                methods=["GET", "POST"],
            )
        ]
    )
    with TestClient(app, base_url="http://127.0.0.1:8000") as browser:
        response = browser.post(
            f"/oauth/connect/{pending_id}",
            data={
                "mode": "session",
                "email": "user@example.com",
                "api_key": "viking-secret",
                "role": "trader",
            },
            follow_redirects=False,
        )

    assert response.status_code == 302
    restarted_provider = VikingOAuthProvider(settings)
    restored = await restarted_provider.get_client(client_id)
    assert restored is not None
    assert restored.token_endpoint_auth_method == "none"
    assert [str(uri) for uri in restored.redirect_uris or []] == [
        "http://127.0.0.1:9876/callback"
    ]


def test_public_metadata_and_complete_oauth_pkce_flow(monkeypatch, tmp_path):
    from app.main import app, oauth_provider

    async def authenticate(_: VikingClient) -> None:
        return None

    monkeypatch.setattr(VikingClient, "authenticate", authenticate)
    oauth_provider._client_store_path = tmp_path / "oauth-clients.json"
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")

    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["oauth_required"] is True

        protected = client.get("/.well-known/oauth-protected-resource/mcp")
        assert protected.status_code == 200
        assert protected.json()["resource"] == "http://127.0.0.1:8000/mcp"

        metadata_response = client.get("/.well-known/oauth-authorization-server")
        assert metadata_response.status_code == 200
        metadata = metadata_response.json()
        assert metadata["authorization_endpoint"] == "http://127.0.0.1:8000/authorize"
        assert metadata["registration_endpoint"] == "http://127.0.0.1:8000/register"

        unauthorized_mcp = client.post(
            "/mcp",
            headers={"Accept": "application/json, text/event-stream"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            },
        )
        assert unauthorized_mcp.status_code == 401
        assert "resource_metadata=" in unauthorized_mcp.headers["www-authenticate"]

        registration = client.post(
            "/register",
            json={
                "client_name": "Codex test",
                "redirect_uris": ["http://127.0.0.1:9876/callback"],
                "token_endpoint_auth_method": "client_secret_post",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "scope": OAUTH_SCOPE,
            },
        )
        assert registration.status_code == 201
        registered = registration.json()

        authorization = client.get(
            "/authorize",
            params={
                "client_id": registered["client_id"],
                "response_type": "code",
                "redirect_uri": "http://127.0.0.1:9876/callback",
                "scope": OAUTH_SCOPE,
                "state": "state-123",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "resource": "http://127.0.0.1:8000/mcp",
            },
            follow_redirects=False,
        )
        assert authorization.status_code == 302
        connect_path = urlparse(authorization.headers["location"]).path

        connected = client.post(
            connect_path,
            data={
                "mode": "local",
                "email": "user@example.com",
                "api_key": "viking-secret",
                "role": "trader",
            },
            follow_redirects=False,
        )
        assert connected.status_code == 302
        code = parse_qs(urlparse(connected.headers["location"]).query)["code"][0]

        token_response = client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "client_id": registered["client_id"],
                "client_secret": registered["client_secret"],
                "code": code,
                "redirect_uri": "http://127.0.0.1:9876/callback",
                "code_verifier": verifier,
                "resource": "http://127.0.0.1:8000/mcp",
            },
        )
        assert token_response.status_code == 200
        token = token_response.json()["access_token"]

        initialized = client.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json, text/event-stream",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "Codex test", "version": "1"},
                },
            },
        )
        assert initialized.status_code == 200
        assert initialized.json()["result"]["serverInfo"]["name"] == "Viking Market Data"
