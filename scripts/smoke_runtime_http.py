"""Run inside a network-disabled container started with its production CMD."""

from __future__ import annotations

import base64
import hashlib
import json
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

BASE = "http://127.0.0.1:8765"
SCOPES = {"viking.read", "viking.portfolio.write"}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = build_opener(NoRedirect())


def request(path: str, *, method: str = "GET", data=None, headers=None):
    payload = None if data is None else json.dumps(data).encode()
    request_headers = {"Accept": "application/json, text/event-stream", **(headers or {})}
    if payload is not None:
        request_headers["Content-Type"] = "application/json"
    req = Request(BASE + path, data=payload, headers=request_headers, method=method)
    try:
        response = OPENER.open(req, timeout=3)
    except HTTPError as exc:
        response = exc
    with response:
        return response.status, response.headers, response.read().decode()


def main() -> None:
    deadline = time.monotonic() + 45
    while True:
        try:
            status, _, body = request("/health")
            if status == 200:
                json.loads(body)
                break
        except (URLError, TimeoutError):
            pass
        if time.monotonic() >= deadline:
            raise RuntimeError("Production CMD did not expose /health on PORT=8765")
        time.sleep(0.25)
    print("PASS production CMD / PORT override / health")
    assert request("/setup")[0] == 200
    for path in ("/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"):
        status, _, body = request(path)
        value = json.loads(body)
        assert status == 200 and value["resource"] == BASE + "/mcp"
        assert set(value["scopes_supported"]) == SCOPES
    status, _, body = request("/.well-known/oauth-authorization-server")
    metadata = json.loads(body)
    assert status == 200 and set(metadata["scopes_supported"]) == SCOPES
    assert metadata["registration_endpoint"] == BASE + "/register"
    status, headers, _ = request(
        "/mcp", method="POST", data={"jsonrpc": "2.0", "id": 1, "method": "initialize"}
    )
    assert status == 401
    assert 'resource_metadata="' in headers["WWW-Authenticate"]
    assert 'scope="viking.read"' not in headers["WWW-Authenticate"]
    print("PASS public OAuth metadata / unauthenticated 401")
    status, headers, _ = request(
        "/mcp",
        method="OPTIONS",
        headers={
            "Origin": "https://k1forge.com",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert status == 200 and headers["Access-Control-Allow-Origin"] == "https://k1forge.com"
    status, _, body = request(
        "/register",
        method="POST",
        data={
            "client_name": "container-smoke-no-credentials",
            "redirect_uris": ["http://127.0.0.1:9999/callback"],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        },
    )
    client = json.loads(body)
    assert status == 201 and set(client["scope"].split()) == SCOPES
    verifier = "container-smoke-verifier-" * 3
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    status, headers, _ = request(
        "/authorize?"
        + urlencode(
            {
                "response_type": "code",
                "client_id": client["client_id"],
                "redirect_uri": "http://127.0.0.1:9999/callback",
                "state": "container-smoke",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "resource": BASE + "/mcp",
            }
        )
    )
    assert status in (302, 303, 307)
    location = headers["Location"]
    assert location.startswith(BASE + "/oauth/connect/")
    status, _, body = request(location.removeprefix(BASE))
    assert status == 200 and "access_mode" in body
    assert "allow_portfolio_writes" in body and "Только чтение" in body
    print("PASS browser CORS / public DCR without scopes / OAuth consent page")
    print("No Viking credentials, token issuance or portfolio commands were used.")


if __name__ == "__main__":
    main()
