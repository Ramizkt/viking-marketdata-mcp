"""MCP entry points for the narrowly allowlisted portfolio controls."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Annotated, Any

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field, StrictBool

from app.auth_compat import WRITE_TOOL_NAMES as WRITE_TOOL_NAMES
from app.auth_compat import write_auth_error
from app.config import Settings
from app.oauth import PORTFOLIO_WRITE_SCOPE
from app.portfolio_control import (
    MAX_TARGETS,
    PortfolioControlService,
    PortfolioTarget,
    Side,
    UserFields,
    validate_controls,
)
from app.service import MarketDataService
from app.viking_client import VikingAPIError

Targets = Annotated[list[PortfolioTarget], Field(min_length=1, max_length=MAX_TARGETS)]

WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True)
COMMON = (
    " По умолчанию dry_run=true: только предпросмотр без изменений. Исполнение требует "
    "dry_run=false, confirm=true и отдельного OAuth-разрешения viking.portfolio.write. "
    "Перед исполнением пользователь должен явно подтвердить эту операцию и точные портфели. "
    "accepted означает подтверждение API, а не проверенное состояние торговли или снятие заявок. "
    "При outcome_unknown не повторяй команду автоматически; проверь состояние."
)


def register_portfolio_control_tools(
    mcp: FastMCP, *, settings: Settings, service_factory: Callable[[], MarketDataService]
) -> None:
    async def run(*, dry_run: bool, confirm: bool, **operation: Any) -> CallToolResult:
        try:
            validate_controls(dry_run, confirm)
            if not dry_run:
                if not settings.viking_portfolio_writes_enabled:
                    raise PermissionError("Portfolio writes are disabled by the server administrator")
                token = get_access_token()
                if token is None or PORTFOLIO_WRITE_SCOPE not in token.scopes:
                    return write_auth_error(settings)
            service = service_factory()
            result = await PortfolioControlService(service.client).run(
                **operation, dry_run=dry_run, confirm=confirm
            )
            return CallToolResult(
                content=[TextContent(type="text", text=json.dumps(result, ensure_ascii=False))],
                structuredContent=result,
                isError=result["has_errors"],
            )
        except (ValueError, RuntimeError, PermissionError, ConnectionError, TimeoutError) as exc:
            result = {"status": "error", "error_type": type(exc).__name__, "message": str(exc)}
            if isinstance(exc, VikingAPIError):
                result.update(code=exc.code, api_response=exc.response)
            return CallToolResult(
                content=[TextContent(type="text", text=json.dumps(result, ensure_ascii=False))],
                structuredContent=result,
                isError=True,
            )

    @mcp.tool(
        title="Изменить пользовательские поля портфеля",
        description=(
            "Частично изменяет только uf0..uf19 одного портфеля. Число — сокращение для {v: число}; "
            "объект принимает v (число), c (подпись) или оба ключа. Неуказанные ключи сохраняются. "
            "Ограничения и редактируемость проверяются по актуальному шаблону. "
            "Пользовательские поля могут влиять на торговые формулы. "
            "После записи проверь нужные поля через get_current_portfolio_data." + COMMON
        ),
        annotations=WRITE,
    )
    async def update_portfolio_user_fields(
        robot_id: str,
        portfolio: str,
        fields: UserFields,
        dry_run: StrictBool = True,
        confirm: StrictBool = False,
    ) -> CallToolResult:
        return await run(
            targets=[{"robot_id": robot_id, "portfolio": portfolio}],
            action="user_fields",
            fields=fields,
            dry_run=dry_run,
            confirm=confirm,
        )

    @mcp.tool(
        title="Выключить re_sell/re_buy портфеля",
        description=(
            "side=sell устанавливает только re_sell=false; side=buy — только re_buy=false; "
            "side=both (по умолчанию) — оба флага false. Второй флаг при выборе sell/buy "
            "не передаётся в запросе и не изменяется этой командой. "
            "Никогда не включает торговлю. Расписание и формулы не отключаются и могут снова "
            "изменить флаги. Не эквивалентно Hard stop или Stop formulas." + COMMON
        ),
        annotations=WRITE,
    )
    async def stop_portfolio_trading(
        robot_id: str,
        portfolio: str,
        side: Side = "both",
        dry_run: StrictBool = True,
        confirm: StrictBool = False,
    ) -> CallToolResult:
        return await run(
            targets=[{"robot_id": robot_id, "portfolio": portfolio}],
            action="stop",
            side=side,
            dry_run=dry_run,
            confirm=confirm,
        )

    @mcp.tool(
        title="Stop portfolios",
        description=(
            "Для явно перечисленных портфелей выбирает направление остановки: side=sell — "
            "только re_sell=false; side=buy — только re_buy=false; side=both (по умолчанию) — "
            "оба флага false. Выбор применяется ко всем targets. Невыбранный флаг не "
            "передаётся в запросе и не изменяется этой командой. Никогда не включает торговлю. "
            "Заявки второй ноги продолжают работать. Расписание и формулы не отключаются. "
            "До 200 точных пар robot_id/portfolio, без wildcard и дубликатов. Пакет не атомарный; "
            "для каждого портфеля возвращается отдельный результат. Позиции не закрываются." + COMMON
        ),
        annotations=WRITE,
    )
    async def stop_portfolios(
        targets: Targets,
        side: Side = "both",
        dry_run: StrictBool = True,
        confirm: StrictBool = False,
    ) -> CallToolResult:
        return await run(targets=targets, action="stop", side=side, dry_run=dry_run, confirm=confirm)

    @mcp.tool(
        title="Hard stop portfolios",
        description=(
            "Отправляет portfolio.hard_stop каждому из явно перечисленных портфелей: останавливает "
            "торговлю и запрашивает снятие заявок обеих ног; расписание отключается, формулы остаются. "
            "До 200 точных пар robot_id/portfolio. Пакет не атомарный; позиции не закрываются. "
            "Не заменяй этой операцией обычный Stop без явного согласия пользователя." + COMMON
        ),
        annotations=WRITE,
    )
    async def hard_stop_portfolios(
        targets: Targets,
        dry_run: StrictBool = True,
        confirm: StrictBool = False,
    ) -> CallToolResult:
        return await run(targets=targets, action="hard_stop", dry_run=dry_run, confirm=confirm)

    @mcp.tool(
        title="Stop formulas",
        description=(
            "Отправляет portfolio.formulas_stop каждому из явно перечисленных портфелей: останавливает "
            "торговлю и отключает формулы. Меняет формульные режимы на константы/Standard; включение "
            "формул обратно требует ручной настройки. До 200 точных пар robot_id/portfolio. "
            "Пакет не атомарный; позиции не закрываются. Требует явного выбора именно Stop formulas, "
            "не используй как автоматическое усиление другой остановки." + COMMON
        ),
        annotations=WRITE,
    )
    async def stop_portfolio_formulas(
        targets: Targets,
        dry_run: StrictBool = True,
        confirm: StrictBool = False,
    ) -> CallToolResult:
        return await run(targets=targets, action="stop_formulas", dry_run=dry_run, confirm=confirm)
