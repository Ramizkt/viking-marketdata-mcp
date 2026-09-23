"""OAuth interoperability without changing Viking operations or read-only contracts."""
from __future__ import annotations

import json
from typing import Any

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.routes import build_metadata, cors_middleware, create_protected_resource_routes
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent, Tool, ToolAnnotations
from pydantic import AnyHttpUrl
from starlette.datastructures import Headers, MutableHeaders
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import Settings
from app.oauth import OAUTH_SCOPE, PORTFOLIO_WRITE_SCOPE, VikingOAuthProvider

WRITE_TOOL_NAMES = frozenset({
    "update_portfolio_user_fields", "stop_portfolio_trading", "stop_portfolios",
    "hard_stop_portfolios", "stop_portfolio_formulas",
})
MAX_INSPECT_BYTES = 2 * 1024 * 1024


def offered_scopes(settings: Settings) -> list[str]:
    """An advertised/requestable upper bound, never an automatic user grant."""
    return [OAUTH_SCOPE, PORTFOLIO_WRITE_SCOPE] if settings.viking_portfolio_writes_enabled else [OAUTH_SCOPE]


def challenge(settings: Settings, *, write: bool = False, invalid: bool = False) -> str:
    metadata = f"{settings.resolved_public_base_url}/.well-known/oauth-protected-resource/mcp"
    value = f'Bearer resource_metadata="{metadata}"'
    if write:
        value += (
            f', error="insufficient_scope", scope="{OAUTH_SCOPE} {PORTFOLIO_WRITE_SCOPE}", '
            'error_description="Reauthorize this connection and explicitly consent to portfolio writes"'
        )
    elif invalid:
        value += ', error="invalid_token", error_description="Authorization is missing, invalid or expired"'
    # No scope on initial 401: general-purpose clients use advertised scopes and
    # the authorization page lets the human grant only a subset (read by default).
    return value


def write_auth_error(settings: Settings) -> CallToolResult:
    result = {
        "status": "error", "error_type": "PermissionError", "code": "insufficient_scope",
        "message": "This OAuth grant is read-only. Reauthorize with viking.portfolio.write and consent in the browser.",
        "required_scopes": [OAUTH_SCOPE, PORTFOLIO_WRITE_SCOPE],
        "reauthorization_required": True, "operation_sent": False,
    }
    return CallToolResult(
        content=[TextContent(type="text", text=result["message"])],
        structuredContent=result,
        isError=True,
        _meta={"mcp/www_authenticate": [challenge(settings, write=True)]},
    )


class CompatibleFastMCP(FastMCP):
    async def list_tools(self) -> list[Tool]:
        tools = await super().list_tools()
        result = []
        for tool in tools:
            scopes = [OAUTH_SCOPE]
            if tool.name in WRITE_TOOL_NAMES:
                scopes.append(PORTFOLIO_WRITE_SCOPE)
            schemes = [{"type": "oauth2", "scopes": scopes}]
            payload = tool.model_dump(by_alias=True, exclude_none=True)
            payload["securitySchemes"] = schemes
            payload["_meta"] = {**(payload.get("_meta") or {}), "securitySchemes": schemes}
            result.append(Tool.model_validate(payload))
        return result


def metadata_routes(settings: Settings) -> list[Route]:
    issuer = AnyHttpUrl(settings.resolved_public_base_url)
    resource = AnyHttpUrl(f"{settings.resolved_public_base_url}/mcp")
    scopes = offered_scopes(settings)
    metadata = build_metadata(
        issuer, AnyHttpUrl(f"{settings.resolved_public_base_url}/setup"),
        ClientRegistrationOptions(enabled=True, valid_scopes=scopes, default_scopes=scopes),
        RevocationOptions(),
    )
    # The existing DCR/token handlers support public clients; advertise this too.
    metadata.token_endpoint_auth_methods_supported = ["none", "client_secret_post", "client_secret_basic"]

    async def authorization_metadata(_: Request) -> JSONResponse:
        return JSONResponse(metadata.model_dump(mode="json", exclude_none=True), headers={"Cache-Control": "no-store"})

    routes = [Route(
        "/.well-known/oauth-authorization-server",
        endpoint=cors_middleware(authorization_metadata, ["GET", "OPTIONS"]), methods=["GET", "OPTIONS"],
    )]
    protected = create_protected_resource_routes(
        resource_url=resource, authorization_servers=[issuer], scopes_supported=scopes,
        resource_name="Viking MCP", resource_documentation=AnyHttpUrl(f"{settings.resolved_public_base_url}/setup"),
    )
    routes.extend(protected)
    routes.append(Route(
        "/.well-known/oauth-protected-resource", endpoint=protected[0].endpoint, methods=["GET", "OPTIONS"],
    ))
    return routes


def register_auth_status(mcp: FastMCP, settings: Settings) -> None:
    @mcp.tool(
        title="Права текущего MCP-подключения",
        description=(
            "Показывает выданные OAuth scope и возможность исполнения записи на стороне MCP, "
            "без обращения к Viking и без изменений портфелей. Не возвращает токены, API key или email. "
            "can_write не подтверждает права роли Viking на конкретный портфель."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True),
    )
    async def get_authorization_status() -> dict[str, Any]:
        token = get_access_token()
        scopes = sorted(token.scopes) if token else []
        enabled = settings.viking_portfolio_writes_enabled
        has_write = PORTFOLIO_WRITE_SCOPE in scopes
        return {
            "authenticated": token is not None, "scopes": scopes,
            "portfolio_writes_enabled": enabled,
            "can_write": enabled and has_write and OAUTH_SCOPE in scopes,
            "reauthorization_required": enabled and not has_write,
            "reason": "server_disabled" if not enabled else "scope_missing" if not has_write else "authorized",
            "required_write_scopes": [OAUTH_SCOPE, PORTFOLIO_WRITE_SCOPE],
            "viking_role_permissions_verified": False,
            "notes": [
                "A read-only registration may require reconnecting/re-registering once with the advertised scopes.",
                "Authorization does not execute/replay a pending operation; get explicit consent for the operation.",
            ],
        }


class OAuthCompatibilityMiddleware:
    """Emit HTTP auth challenges before any confirmed write, keeping its MCP error body.

    Auth checks inside tools remain authoritative for direct/non-HTTP calls. No
    user-agent heuristics or automatic replay. Unchanged JSON is replayed to the
    SDK, including frames not inspected because they exceed the inspection cap.
    """

    def __init__(self, app: ASGIApp, *, settings: Settings, provider: VikingOAuthProvider):
        self.app, self.settings, self.provider = app, settings, provider

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] not in {"/mcp", "/mcp/"}:
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        buffered: list[Message] = []
        body = bytearray()
        inspect = scope["method"] == "POST" and self.settings.viking_portfolio_writes_enabled
        if inspect and headers.get("content-type", "").split(";", 1)[0].strip() == "application/json":
            while True:
                frame = await receive()
                buffered.append(frame)
                if frame["type"] == "http.disconnect":
                    return
                body.extend(frame.get("body", b""))
                if not frame.get("more_body", False) or len(body) > MAX_INSPECT_BYTES:
                    break
            if len(body) <= MAX_INSPECT_BYTES:
                try:
                    message = json.loads(body)
                except (ValueError, UnicodeError, RecursionError):
                    message = None
                if self._is_confirmed_write(message):
                    bearer = headers.get("authorization", "").split()
                    if len(bearer) == 2 and bearer[0].lower() == "bearer":
                        token = await self.provider.load_access_token(bearer[1])
                        if token and OAUTH_SCOPE in token.scopes and PORTFOLIO_WRITE_SCOPE not in token.scopes:
                            result = write_auth_error(self.settings)
                            response = JSONResponse(
                                {"jsonrpc": "2.0", "id": message["id"], "result": result.model_dump(by_alias=True, exclude_none=True)},
                                status_code=403,
                                headers={"WWW-Authenticate": challenge(self.settings, write=True), "Cache-Control": "no-store"},
                            )
                            await response(scope, receive, send)
                            return
        index = 0

        async def replay() -> Message:
            nonlocal index
            if index < len(buffered):
                frame = buffered[index]
                index += 1
                return frame
            return await receive()

        async def rewrite_challenge(message: Message) -> None:
            if message["type"] == "http.response.start" and message["status"] == 401:
                response_headers = MutableHeaders(scope=message)
                response_headers["WWW-Authenticate"] = challenge(self.settings, invalid=bool(headers.get("authorization")))
                response_headers["Cache-Control"] = "no-store"
            await send(message)

        await self.app(scope, replay, rewrite_challenge)

    @staticmethod
    def _is_confirmed_write(message: Any) -> bool:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or "id" not in message:
            return False
        params = message.get("params")
        if message.get("method") != "tools/call" or not isinstance(params, dict):
            return False
        name, args = params.get("name"), params.get("arguments")
        return (
            isinstance(name, str) and name in WRITE_TOOL_NAMES and isinstance(args, dict)
            and args.get("dry_run") is False and args.get("confirm") is True
        )
