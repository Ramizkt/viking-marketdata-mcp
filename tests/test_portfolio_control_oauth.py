from __future__ import annotations

from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import pytest
from mcp.server.auth.provider import AuthorizationParams, AuthorizeError, TokenError
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from app.config import Settings
from app.oauth import OAUTH_SCOPE, PORTFOLIO_WRITE_SCOPE, VikingOAuthProvider
from app.viking_client import VikingClient


def config(tmp_path, enabled=True):
    return Settings(
        public_base_url="http://127.0.0.1:8000",
        export_signing_key="fixture-only-encryption-key",
        oauth_client_store_path=tmp_path / "oauth-clients.json",
        viking_portfolio_writes_enabled=enabled,
    )


def oauth_client(scopes):
    return OAuthClientInformationFull(
        client_id="fixture-client",
        client_secret=None,
        redirect_uris=[AnyUrl("http://127.0.0.1:9876/callback")],
        token_endpoint_auth_method="none",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope=" ".join(scopes),
    )


def params(scopes):
    return AuthorizationParams(
        state="fixture-state",
        scopes=scopes,
        code_challenge="fixture-challenge",
        redirect_uri=AnyUrl("http://127.0.0.1:9876/callback"),
        redirect_uri_provided_explicitly=True,
        resource="http://127.0.0.1:8000/mcp",
    )


@pytest.mark.parametrize("mode", ["session", "local"])
@pytest.mark.parametrize("write", [False, True])
async def test_browser_consent_and_grant_round_trip(tmp_path, monkeypatch, mode, write):
    authenticate = AsyncMock()
    monkeypatch.setattr(VikingClient, "authenticate", authenticate)
    provider = VikingOAuthProvider(config(tmp_path))
    scopes = [OAUTH_SCOPE, PORTFOLIO_WRITE_SCOPE] if write else [OAUTH_SCOPE]
    client = oauth_client(scopes)
    await provider.register_client(client)
    url = await provider.authorize(client, params(scopes))
    pending_id = url.rsplit("/", 1)[-1]
    app = Starlette(
        routes=[Route("/oauth/connect/{pending_id:str}", provider.connect_page, methods=["GET", "POST"])]
    )
    form = {"mode": mode, "email": "fixture@example.invalid", "api_key": "fixture-only", "role": "trader"}
    with TestClient(app) as browser:
        page = browser.get(f"/oauth/connect/{pending_id}")
        assert ('name="allow_portfolio_writes"' in page.text) is write
        assert 'value="yes" checked' not in page.text
        assert "form-action" not in page.headers["content-security-policy"]
        if write:
            rejected = browser.post(f"/oauth/connect/{pending_id}", data=form, follow_redirects=False)
            assert rejected.status_code == 200
            assert "явное разрешение" in rejected.text
            assert not provider._codes
            authenticate.assert_not_awaited()
        # A forged checkbox on a read-only request must not upgrade its scopes.
        response = browser.post(
            f"/oauth/connect/{pending_id}",
            data={**form, "allow_portfolio_writes": "yes"},
            follow_redirects=False,
        )
    assert response.status_code == 302
    code_value = parse_qs(urlparse(response.headers["location"]).query)["code"][0]
    code = await provider.load_authorization_code(client, code_value)
    assert code is not None
    assert code.scopes == scopes
    issued = await provider.exchange_authorization_code(client, code)
    access = await provider.load_access_token(issued.access_token)
    assert access is not None and access.scopes == scopes
    stored = provider.credentials_for_access_token(issued.access_token)
    assert stored is not None and stored.api_key == "fixture-only"
    if mode == "session":
        refresh = await provider.load_refresh_token(client, issued.refresh_token)
        renewed = await provider.exchange_refresh_token(client, refresh, scopes)
        access = await provider.load_access_token(renewed.access_token)
        assert access is not None and access.scopes == scopes
    else:
        restarted = VikingOAuthProvider(config(tmp_path))
        access = await restarted.load_access_token(issued.access_token)
        assert access is not None and access.scopes == scopes
    await provider.close()


@pytest.mark.parametrize(
    "registered, requested, enabled",
    [
        ([OAUTH_SCOPE], [OAUTH_SCOPE, PORTFOLIO_WRITE_SCOPE], True),
        ([OAUTH_SCOPE, PORTFOLIO_WRITE_SCOPE], [OAUTH_SCOPE, PORTFOLIO_WRITE_SCOPE], False),
        ([OAUTH_SCOPE, PORTFOLIO_WRITE_SCOPE], [PORTFOLIO_WRITE_SCOPE], True),
        ([OAUTH_SCOPE, "arbitrary.write"], [OAUTH_SCOPE, "arbitrary.write"], True),
    ],
)
async def test_authorization_cannot_escalate(tmp_path, registered, requested, enabled):
    provider = VikingOAuthProvider(config(tmp_path, enabled))
    with pytest.raises(AuthorizeError):
        await provider.authorize(oauth_client(registered), params(requested))
    assert not provider._pending


async def test_read_refresh_cannot_gain_write_scope(tmp_path):
    provider = VikingOAuthProvider(config(tmp_path))
    from app.credentials import VikingCredentials

    issued = provider._issue_session_tokens(
        credentials=VikingCredentials(email="fixture@example.invalid", api_key="fixture-only", role="trader"),
        client_id="fixture-client",
        scopes=[OAUTH_SCOPE],
        resource=None,
        subject="fixture-subject",
        absolute_expires_at=4_000_000_000,
    )
    client = oauth_client([OAUTH_SCOPE, PORTFOLIO_WRITE_SCOPE])
    refresh = await provider.load_refresh_token(client, issued.refresh_token)
    with pytest.raises(TokenError):
        await provider.exchange_refresh_token(client, refresh, [OAUTH_SCOPE, PORTFOLIO_WRITE_SCOPE])
    assert all(PORTFOLIO_WRITE_SCOPE not in s.access.scopes for s in provider._session_tokens.values())
    await provider.close()
