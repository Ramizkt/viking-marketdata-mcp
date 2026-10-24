"""Authenticated MCP cursor-feed tools; no additional HTTP transport."""

from __future__ import annotations

import re
import time
from typing import Annotated, Any

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.oauth import OAUTH_SCOPE, VikingOAuthProvider
from app.portfolio_feed import PortfolioFeedError, PortfolioFeedManager
from app.viking_client import VikingAPIError, VikingClientPool


class PortfolioTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    robot_id: str | int
    portfolio: str = Field(min_length=1, max_length=200)

    @field_validator("robot_id")
    @classmethod
    def robot_identity(cls, value: str | int) -> str:
        if (
            isinstance(value, bool) or re.fullmatch(r"[0-9]{1,20}", str(value)) is None
            or (isinstance(value, int) and value > 9007199254740991)
        ):
            raise ValueError("robot_id must be a 1..20 digit string or a safe non-negative JSON integer")
        return str(value)


class FeedCursor(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    stream_id: str = Field(min_length=1, max_length=128)
    epoch: str = Field(min_length=1, max_length=128)
    seq: int = Field(ge=0)


def register_feed_tools(
    mcp: FastMCP, *, manager: PortfolioFeedManager,
    provider: VikingOAuthProvider, clients: VikingClientPool,
) -> None:
    annotations = ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=False, openWorldHint=True,
    )

    async def authenticate():
        context = get_access_token()
        access = await provider.load_access_token(context.token) if context else None
        if access is None or OAUTH_SCOPE not in access.scopes:
            raise PermissionError("Valid OAuth viking.read authorization is required")
        credentials = provider.credentials_for_access_token(access.token)
        if credentials is None:
            raise PermissionError("OAuth session is invalid or expired; authenticate again")
        return (access.client_id, credentials.fingerprint), clients.get(credentials), access.expires_at

    def failure(exc: Exception) -> CallToolResult:
        details: dict[str, Any] = {
            "status": "error", "error_type": type(exc).__name__, "message": str(exc),
        }
        if isinstance(exc, VikingAPIError):
            details["feed_error"] = "upstream_rejected"
            details["code"] = exc.code
            details["api_response"] = exc.response
        elif isinstance(exc, PortfolioFeedError):
            details["feed_error"] = exc.code
        elif isinstance(exc, PermissionError):
            details["feed_error"] = "authorization_required"
        elif isinstance(exc, (ConnectionError, TimeoutError)):
            details["feed_error"] = "upstream_unavailable"
        else:
            details["feed_error"] = "invalid_request" if isinstance(exc, ValueError) else "upstream_failed"
        return CallToolResult(
            content=[TextContent(type="text", text=str(exc))], structuredContent=details, isError=True,
        )

    def success(result: dict[str, Any]) -> CallToolResult:
        return CallToolResult(
            content=[TextContent(type="text", text="Portfolio feed response.")],
            structuredContent=result,
        )

    @mcp.tool(annotations=annotations)
    async def open_portfolio_feed(
        portfolios: Annotated[list[PortfolioTarget], Field(min_length=1, max_length=100)],
        feed_id: str | None = None,
        lease_seconds: Annotated[int, Field(ge=1, le=300, strict=True)] = 300,
    ) -> CallToolResult:
        """Open a shared portfolio feed, or renew feed_id with exactly the same portfolio set.

        Returns full snapshots and independent epoch/seq cursors. Keep feed_id private.
        Lease is bounded by 300 seconds and OAuth expiry. Renew before it expires.
        No Viking credentials or legacy subscription_id are arguments or returned.
        """
        try:
            owner, client, token_expiry = await authenticate()
            expiry = min(time.time() + lease_seconds, token_expiry or float("inf"))
            result = await manager.open(
                owner, client, [(str(p.robot_id), p.portfolio) for p in portfolios],
                feed_id=feed_id, expires_at=expiry,
            )
            try:
                await authenticate()  # No data leaves a setup call after token expiry/revocation.
            except PermissionError:
                if feed_id is None:
                    await manager.close_feed(owner, result["feed_id"])
                raise
            return success(result)
        except (ValueError, RuntimeError, ConnectionError, TimeoutError, PermissionError) as exc:
            return failure(exc)

    @mcp.tool(annotations=annotations)
    async def read_portfolio_feed(
        feed_id: str,
        cursors: Annotated[list[FeedCursor], Field(min_length=1, max_length=100)],
        wait_seconds: Annotated[float, Field(ge=0, le=30)] = 25,
        max_events: Annotated[int, Field(ge=1, le=500, strict=True)] = 100,
    ) -> CallToolResult:
        """Non-destructive multiplex read; wakes on ANY portfolio with events after its cursor.

        Supply one cursor per stream. Apply items in order; snapshot replaces state and
        sets its epoch/seq. Gap or reconnect returns an explicit snapshot reset.
        Advance only cursors of returned frames; retrying old cursors replays retained data.
        A timeout returns empty items. This call does not renew the bounded feed lease.
        """
        try:
            owner, _, _ = await authenticate()
            result = await manager.read(
                owner, feed_id, [c.model_dump() for c in cursors],
                wait_seconds=wait_seconds, max_events=max_events,
            )
            await authenticate()  # Reject session revocation while long-poll was waiting.
            return success(result)
        except (ValueError, RuntimeError, ConnectionError, TimeoutError, PermissionError) as exc:
            return failure(exc)

    @mcp.tool(annotations=annotations)
    async def close_portfolio_feed(feed_id: str) -> CallToolResult:
        """Release only this OAuth client's feed lease; last lease unsubscribes upstream."""
        try:
            owner, _, _ = await authenticate()
            return success(await manager.close_feed(owner, feed_id))
        except (ValueError, RuntimeError, ConnectionError, TimeoutError, PermissionError) as exc:
            return failure(exc)
