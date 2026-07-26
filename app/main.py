from __future__ import annotations

import contextlib
import logging
from datetime import datetime
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, ResourceLink, TextContent, ToolAnnotations
from pydantic import AnyUrl, Field
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route

from app.config import get_settings
from app.credentials import (
    REQUIRED_HEADERS,
    credentials_from_scope,
    require_request_credentials,
    reset_request_credentials,
    set_request_credentials,
)
from app.export_store import ExportStore
from app.service import Aggregation, Delivery, MarketDataService
from app.viking_client import VikingAPIError, VikingClientPool

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

settings = get_settings()
viking_clients = VikingClientPool(settings)
export_store = ExportStore(settings)

mcp = FastMCP(
    "Viking Market Data",
    instructions=(
        "Сначала вызывай list_available_portfolios. Для выгрузки выбирай портфель с "
        "history_available=true. В get_portfolio_data даты всегда передавай с часовым поясом. "
        "Используй delivery=auto: небольшие выборки вернутся inline, большие — CSV-файлом. "
        "Если пользователь не назвал поля, используй buy, sell и pos. Сервер только читает данные. "
        "Каждый клиент обязан передавать собственные Viking credentials в заголовках "
        "X-Viking-Email, X-Viking-API-Key и X-Viking-Role."
    ),
    host="0.0.0.0",
    port=settings.port,
    streamable_http_path="/mcp",
    stateless_http=True,
    json_response=True,
)

READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)


def _service_for_request() -> MarketDataService:
    credentials = require_request_credentials()
    return MarketDataService(settings, viking_clients.get(credentials), export_store)


@mcp.tool(
    title="Доступные портфели",
    description=(
        "Возвращает портфели, доступные текущей роли Viking. Для каждого портфеля показывает "
        "robot_id, имя, владельца и history_available. Установи history_only=true, если нужно "
        "показать только портфели, из которых можно выгружать исторические данные."
    ),
    annotations=READ_ONLY,
)
async def list_available_portfolios(history_only: bool = False) -> dict[str, Any]:
    return await _service_for_request().list_available_portfolios(history_only=history_only)


@mcp.tool(
    title="История портфеля",
    description=(
        "Получает историю полей портфеля за период. date_from/date_to должны быть ISO 8601 "
        "с часовым поясом. Доступные агрегации: raw, 10s, 1m, 5m, 10m, 1h, 6h, 24h. "
        "delivery: auto, inline, file, stream или summary. Сервер может безопасно заменить "
        "inline/stream на file при большом объёме. По умолчанию поля: buy, sell, pos."
    ),
    annotations=READ_ONLY,
)
async def get_portfolio_data(
    robot_id: Annotated[str, Field(min_length=1, description="Идентификатор робота")],
    portfolio: Annotated[str, Field(min_length=1, description="Имя портфеля")],
    date_from: Annotated[datetime, Field(description="Начало периода с часовым поясом")],
    date_to: Annotated[datetime, Field(description="Конец периода с часовым поясом")],
    fields: Annotated[
        list[str] | None,
        Field(description="Поля: buy, sell, pos, fin_res, price_s, price_b, lim_s, lim_b, uf0..uf19"),
    ] = None,
    aggregation: Aggregation = "raw",
    delivery: Delivery = "auto",
    preview_rows: Annotated[int, Field(ge=0, le=100)] = 5,
) -> CallToolResult:
    try:
        result = await _service_for_request().get_portfolio_data(
            robot_id=robot_id,
            portfolio=portfolio,
            date_from=date_from,
            date_to=date_to,
            fields=fields,
            aggregation=aggregation,
            delivery=delivery,
            preview_rows=preview_rows,
        )
    except (ValueError, RuntimeError, VikingAPIError) as exc:
        logger.warning("Portfolio data request failed: %s", exc)
        return CallToolResult(
            content=[TextContent(type="text", text=str(exc))],
            structuredContent={"status": "error", "message": str(exc)},
            isError=True,
        )

    content = [TextContent(type="text", text=result.summary)]
    if result.exported_file is not None:
        content.append(
            ResourceLink(
                type="resource_link",
                uri=AnyUrl(result.exported_file.download_url),
                name=result.exported_file.filename,
                description="CSV с историческими данными портфеля",
                mimeType="text/csv",
                size=result.exported_file.size_bytes,
            )
        )
    return CallToolResult(content=content, structuredContent=result.structured)


async def health(_: Request) -> JSONResponse:
    return JSONResponse(
        {
            "status": "ok",
            "service": "viking-marketdata-mcp",
            "mcp_path": "/mcp",
            "credential_mode": "per_request_headers",
            "server_credentials_stored": False,
            "required_mcp_headers": list(REQUIRED_HEADERS),
        }
    )


async def download(request: Request):
    filename = request.path_params["filename"]
    try:
        expires_at = int(request.query_params.get("expires", "0"))
    except ValueError:
        return PlainTextResponse("Invalid download link", status_code=403)
    signature = request.query_params.get("sig", "")
    path = export_store.resolve_download(filename, expires_at, signature)
    if path is None:
        return PlainTextResponse("Download link is invalid or expired", status_code=403)
    return FileResponse(path, media_type="text/csv", filename=filename.split("--", 1)[-1])


class VikingCredentialsMiddleware:
    """Attach caller-supplied Viking credentials to each stateless MCP request."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path", "").startswith("/mcp"):
            credentials, missing = credentials_from_scope(scope)
            if credentials is None:
                response = JSONResponse(
                    {
                        "error": "Missing Viking credentials",
                        "missing_headers": missing,
                    },
                    status_code=401,
                )
                await response(scope, receive, send)
                return
            token = set_request_credentials(credentials)
            try:
                await self.app(scope, receive, send)
            finally:
                reset_request_credentials(token)
            return
        await self.app(scope, receive, send)


_mcp_http_app = mcp.streamable_http_app()


@contextlib.asynccontextmanager
async def lifespan(_: Starlette):
    async with mcp.session_manager.run():
        yield
    await viking_clients.close()


_starlette_app = Starlette(
    routes=[
        Route("/health", health, methods=["GET"]),
        Route("/downloads/{filename:str}", download, methods=["GET"]),
        Mount("/", app=_mcp_http_app),
    ],
    lifespan=lifespan,
)
app = VikingCredentialsMiddleware(_starlette_app)


def run() -> None:
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=settings.port)


if __name__ == "__main__":
    run()
