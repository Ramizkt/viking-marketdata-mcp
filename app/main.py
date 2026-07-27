from __future__ import annotations

import contextlib
import logging
from datetime import datetime
from typing import Annotated, Any

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, ResourceLink, TextContent, ToolAnnotations
from pydantic import AnyHttpUrl, AnyUrl, Field
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route

from app.config import get_settings
from app.export_store import ExportStore
from app.oauth import OAUTH_SCOPE, VikingOAuthProvider
from app.onboarding import setup_page
from app.service import Aggregation, Delivery, MarketDataService
from app.viking_client import VikingAPIError, VikingClientPool, VikingProtocolError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

settings = get_settings()
viking_clients = VikingClientPool(settings)
export_store = ExportStore(settings)
oauth_provider = VikingOAuthProvider(settings)

issuer_url = AnyHttpUrl(settings.resolved_public_base_url)
resource_url = AnyHttpUrl(f"{settings.resolved_public_base_url}/mcp")

mcp = FastMCP(
    "Viking Market Data",
    instructions=(
        "Пользователь уже прошёл безопасную браузерную OAuth-авторизацию. Никогда не проси "
        "email или API key в чате. Сначала вызывай list_available_portfolios. "
        "Текущее полное состояние портфеля получай через get_current_portfolio_data. "
        "Для исторической выгрузки выбирай портфель с history_available=true; "
        "в get_portfolio_data даты всегда передавай с часовым поясом. "
        "Используй delivery=auto: небольшие выборки вернутся inline, большие — CSV-файлом. "
        "Если пользователь просит следить за изменениями списка портфелей, вызови "
        "subscribe_available_portfolios, читай события через get_available_portfolio_updates "
        "и обязательно заверши подписку через unsubscribe_available_portfolios. "
        "Для изменений полей одного портфеля используй subscribe_portfolio, "
        "get_portfolio_updates и unsubscribe_portfolio. "
        "Если пользователь не назвал поля, используй buy, sell и pos. Сервер только читает данные. "
        "Credentials не входят в аргументы MCP-инструментов."
    ),
    auth_server_provider=oauth_provider,
    auth=AuthSettings(
        issuer_url=issuer_url,
        service_documentation_url=AnyHttpUrl(f"{settings.resolved_public_base_url}/setup"),
        client_registration_options=ClientRegistrationOptions(
            enabled=True,
            valid_scopes=[OAUTH_SCOPE],
            default_scopes=[OAUTH_SCOPE],
        ),
        required_scopes=[OAUTH_SCOPE],
        resource_server_url=resource_url,
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

SUBSCRIPTION_TOOL = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
)

SUBSCRIPTION_ERRORS = (
    ValueError,
    RuntimeError,
    VikingAPIError,
    VikingProtocolError,
    ConnectionError,
    TimeoutError,
)


def _error_result(exc: BaseException) -> CallToolResult:
    details: dict[str, Any] = {
        "status": "error",
        "error_type": type(exc).__name__,
        "message": str(exc),
    }
    if isinstance(exc, VikingAPIError):
        details["code"] = exc.code
        if exc.response is not None:
            details["api_response"] = exc.response
    return CallToolResult(
        content=[TextContent(type="text", text=str(exc))],
        structuredContent=details,
        isError=True,
    )


def _service_for_request() -> MarketDataService:
    access_token = get_access_token()
    if access_token is None:
        raise RuntimeError("OAuth authorization is required.")
    credentials = oauth_provider.credentials_for_access_token(access_token.token)
    if credentials is None:
        raise RuntimeError("OAuth session is invalid or expired. Authenticate again.")
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
    title="Подписаться на доступные портфели",
    description=(
        "Создаёт подписку Viking available_portfolio_list.subscribe. Возвращает полный "
        "первоначальный снапшот: subscription_id/eid, type, ts, r/result, data и все портфели "
        "с robot_id, portfolio, owner. Сохрани subscription_id для чтения обновлений и отписки."
    ),
    annotations=SUBSCRIPTION_TOOL,
)
async def subscribe_available_portfolios() -> CallToolResult:
    try:
        result = await _service_for_request().subscribe_available_portfolios()
    except SUBSCRIPTION_ERRORS as exc:
        logger.warning("Available portfolios subscription failed: %s", exc)
        return _error_result(exc)
    return CallToolResult(
        content=[
            TextContent(
                type="text",
                text=(
                    f"Подписка создана. Получено портфелей: "
                    f"{len(result['portfolios_add'])}."
                ),
            )
        ],
        structuredContent=result,
    )


@mcp.tool(
    title="Получить обновления доступных портфелей",
    description=(
        "Возвращает накопленные события активной подписки: добавленные и удалённые портфели. "
        "Каждое событие содержит type, eid, ts, r/result, data, portfolios_add и portfolios_del. "
        "wait_seconds=0 проверяет сразу, значение до 30 секунд позволяет дождаться события."
    ),
    annotations=SUBSCRIPTION_TOOL,
)
async def get_available_portfolio_updates(
    subscription_id: Annotated[str, Field(min_length=1)],
    wait_seconds: Annotated[float, Field(ge=0, le=30)] = 0,
    max_events: Annotated[int, Field(ge=1, le=500)] = 100,
) -> CallToolResult:
    try:
        result = await _service_for_request().get_available_portfolio_updates(
            subscription_id=subscription_id,
            wait_seconds=wait_seconds,
            max_events=max_events,
        )
    except SUBSCRIPTION_ERRORS as exc:
        logger.warning("Reading available portfolios updates failed: %s", exc)
        return _error_result(exc)
    return CallToolResult(
        content=[
            TextContent(
                type="text", text=f"Получено событий подписки: {result['event_count']}."
            )
        ],
        structuredContent=result,
    )


@mcp.tool(
    title="Отписаться от доступных портфелей",
    description=(
        "Вызывает available_portfolio_list.unsubscribe для указанного subscription_id "
        "и возвращает полный ответ Viking."
    ),
    annotations=SUBSCRIPTION_TOOL,
)
async def unsubscribe_available_portfolios(
    subscription_id: Annotated[str, Field(min_length=1)],
) -> CallToolResult:
    try:
        result = await _service_for_request().unsubscribe_available_portfolios(
            subscription_id=subscription_id
        )
    except SUBSCRIPTION_ERRORS as exc:
        logger.warning("Available portfolios unsubscribe failed: %s", exc)
        return _error_result(exc)
    return CallToolResult(
        content=[TextContent(type="text", text="Подписка успешно закрыта.")],
        structuredContent=result,
    )


@mcp.tool(
    title="Текущие данные портфеля",
    description=(
        "Возвращает полный текущий снапшот портфеля через Viking portfolio.subscribe и сразу "
        "закрывает служебную подписку через portfolio.unsubscribe. Сохраняет без фильтрации "
        "все поля шаблона портфеля, uf0..uf19, timetable и все поля securities."
    ),
    annotations=READ_ONLY,
)
async def get_current_portfolio_data(
    robot_id: Annotated[str, Field(min_length=1, description="Идентификатор робота")],
    portfolio: Annotated[str, Field(min_length=1, description="Имя портфеля")],
) -> CallToolResult:
    try:
        result = await _service_for_request().get_current_portfolio_data(
            robot_id=robot_id,
            portfolio=portfolio,
        )
    except SUBSCRIPTION_ERRORS as exc:
        logger.warning("Current portfolio data request failed: %s", exc)
        return _error_result(exc)
    securities = result["value"].get("securities", {})
    return CallToolResult(
        content=[
            TextContent(
                type="text",
                text=(
                    f"Получен текущий снапшот {robot_id}/{portfolio}: "
                    f"{len(result['value'])} полей, {len(securities)} инструментов."
                ),
            )
        ],
        structuredContent=result,
    )


@mcp.tool(
    title="Подписаться на портфель",
    description=(
        "Создаёт read-only подписку Viking portfolio.subscribe и возвращает полный начальный "
        "снапшот со всеми динамическими полями портфеля и его securities. Сохрани "
        "subscription_id для чтения обновлений и явной отписки."
    ),
    annotations=SUBSCRIPTION_TOOL,
)
async def subscribe_portfolio(
    robot_id: Annotated[str, Field(min_length=1, description="Идентификатор робота")],
    portfolio: Annotated[str, Field(min_length=1, description="Имя портфеля")],
) -> CallToolResult:
    try:
        result = await _service_for_request().subscribe_portfolio(
            robot_id=robot_id,
            portfolio=portfolio,
        )
    except SUBSCRIPTION_ERRORS as exc:
        logger.warning("Portfolio subscription failed: %s", exc)
        return _error_result(exc)
    securities = result["value"].get("securities", {})
    return CallToolResult(
        content=[
            TextContent(
                type="text",
                text=(
                    f"Подписка на {robot_id}/{portfolio} создана. "
                    f"Получено полей: {len(result['value'])}; "
                    f"инструментов: {len(securities)}."
                ),
            )
        ],
        structuredContent=result,
    )


@mcp.tool(
    title="Получить обновления портфеля",
    description=(
        "Возвращает накопленные снапшоты и частичные обновления активной portfolio.subscribe. "
        "Сохраняет все неизвестные заранее поля, частичные uf0..uf19, изменения securities "
        "и __action=del. wait_seconds=0 проверяет сразу, максимум ожидания — 30 секунд."
    ),
    annotations=SUBSCRIPTION_TOOL,
)
async def get_portfolio_updates(
    subscription_id: Annotated[str, Field(min_length=1)],
    wait_seconds: Annotated[float, Field(ge=0, le=30)] = 0,
    max_events: Annotated[int, Field(ge=1, le=500)] = 100,
) -> CallToolResult:
    try:
        result = await _service_for_request().get_portfolio_updates(
            subscription_id=subscription_id,
            wait_seconds=wait_seconds,
            max_events=max_events,
        )
    except SUBSCRIPTION_ERRORS as exc:
        logger.warning("Reading portfolio updates failed: %s", exc)
        return _error_result(exc)
    return CallToolResult(
        content=[
            TextContent(
                type="text",
                text=(
                    f"Получено событий портфеля: {result['event_count']}. "
                    f"Подписка активна: {result['active']}."
                ),
            )
        ],
        structuredContent=result,
    )


@mcp.tool(
    title="Отписаться от портфеля",
    description=(
        "Вызывает Viking portfolio.unsubscribe для указанного subscription_id и возвращает "
        "полный ответ API."
    ),
    annotations=SUBSCRIPTION_TOOL,
)
async def unsubscribe_portfolio(
    subscription_id: Annotated[str, Field(min_length=1)],
) -> CallToolResult:
    try:
        result = await _service_for_request().unsubscribe_portfolio(
            subscription_id=subscription_id
        )
    except SUBSCRIPTION_ERRORS as exc:
        logger.warning("Portfolio unsubscribe failed: %s", exc)
        return _error_result(exc)
    return CallToolResult(
        content=[TextContent(type="text", text="Подписка на портфель успешно закрыта.")],
        structuredContent=result,
    )


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
            "oauth_required": True,
            "credential_modes": ["session", "local_encrypted_token"],
            "persistent_user_database": False,
            "setup_path": "/setup",
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


_mcp_http_app = mcp.streamable_http_app()


@contextlib.asynccontextmanager
async def lifespan(_: Starlette):
    async with mcp.session_manager.run():
        viking_clients.start()
        oauth_provider.start()
        yield
    await oauth_provider.close()
    await viking_clients.close()


_starlette_app = Starlette(
    routes=[
        Route("/health", health, methods=["GET"]),
        Route("/setup", setup_page, methods=["GET"]),
        Route(
            "/oauth/connect/{pending_id:str}",
            oauth_provider.connect_page,
            methods=["GET", "POST"],
        ),
        Route("/downloads/{filename:str}", download, methods=["GET"]),
        Mount("/", app=_mcp_http_app),
    ],
    lifespan=lifespan,
)
app = _starlette_app


def run() -> None:
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=settings.port)


if __name__ == "__main__":
    run()
