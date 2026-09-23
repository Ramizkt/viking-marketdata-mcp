from urllib.parse import parse_qs, urlparse

import pytest
from test_oauth_compatibility import (
    ARGS,
    BOTH,
    FORM,
    VERIFIER,
    login,
    register,
    rpc,
    start,
)
from test_oauth_compatibility import runtime as runtime

from app.oauth import OAUTH_SCOPE
from app.viking_client import VikingAPIError


def test_old_read_registration_still_defaults_to_read(runtime):
    client = register(runtime, OAUTH_SCOPE)
    token = login(runtime, client, legacy=True)
    assert token["scope"] == OAUTH_SCOPE
    response = rpc(runtime, token["access_token"], "tools/call", {"name": "get_authorization_status"})
    assert response.json()["result"]["structuredContent"]["can_write"] is False


def test_explicit_read_request_is_not_broadened_by_new_registration_defaults(runtime):
    client = register(runtime)
    assert client["scope"].split() == BOTH
    token = login(runtime, client, scope=OAUTH_SCOPE, legacy=True, access="write")
    assert token["scope"] == OAUTH_SCOPE


@pytest.mark.parametrize("mode", ["session", "local"])
def test_authorization_code_created_before_downgrade_cannot_mint_write(runtime, mode):
    rt = runtime
    client = register(rt)
    url = start(rt, client).headers["location"]
    response = rt.http.post(
        url,
        data={**FORM, "mode": mode, "access_mode": "write", "allow_portfolio_writes": "yes"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    code = parse_qs(urlparse(response.headers["location"]).query)["code"][0]
    login(rt, client, access="read")
    exchanged = rt.http.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "client_id": client["client_id"],
            "code": code,
            "redirect_uri": client["redirect_uris"][0],
            "code_verifier": VERIFIER,
        },
    )
    assert exchanged.status_code == 200
    token = exchanged.json()
    assert token["scope"] == OAUTH_SCOPE
    current = rpc(rt, token["access_token"], "tools/call", {"name": "get_authorization_status"})
    assert current.json()["result"]["structuredContent"]["can_write"] is False


def test_unreadable_policy_denies_write_without_breaking_read(runtime):
    rt = runtime
    client = register(rt)
    read = login(rt, client, access="read")
    write = login(rt, client, access="write")
    rt.provider._grants.path.write_bytes(b"not-a-sqlite-database")
    for token in (read, write):
        result = rpc(rt, token["access_token"], "tools/call", {"name": "fixture_read"})
        assert result.status_code == 200 and not result.json()["result"]["isError"]
        status = rpc(rt, token["access_token"], "tools/call", {"name": "get_authorization_status"})
        assert status.json()["result"]["structuredContent"]["can_write"] is False
    rt.viking.execute_portfolio_control.assert_not_awaited()


def test_platform_permission_error_is_not_an_oauth_challenge(runtime):
    rt = runtime
    token = login(rt, register(rt), access="write")
    rt.viking.execute_portfolio_control.side_effect = VikingAPIError(
        "Permission denied", 555, response={"r": "e", "data": {"msg": "Permission denied", "code": 555}}
    )
    result = rpc(rt, token["access_token"], "tools/call", {
        "name": "stop_portfolio_trading", "arguments": {**ARGS, "dry_run": False, "confirm": True},
    })
    assert result.status_code == 200
    value = result.json()["result"]
    assert value["isError"] is True
    assert value["structuredContent"]["items"][0]["status"] == "rejected"
    assert "www-authenticate" not in result.headers
    assert "mcp/www_authenticate" not in value.get("_meta", {})
    assert rt.viking.execute_portfolio_control.await_count == 1


def test_registered_redirect_uri_is_not_replaced_by_client(runtime):
    rt = runtime
    client = register(rt)
    response = rt.http.get("/authorize", params={
        "response_type": "code", "client_id": client["client_id"],
        "redirect_uri": "https://unregistered.example.invalid/callback",
        "scope": " ".join(BOTH), "code_challenge": "fixture", "code_challenge_method": "S256",
    }, follow_redirects=False)
    assert response.status_code == 400
    assert "location" not in response.headers
    rt.authenticate.assert_not_awaited()
