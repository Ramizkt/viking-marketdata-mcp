from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replace_class_method(source: str, name: str, replacement: str) -> str:
    pattern = re.compile(
        rf"^    async def {re.escape(name)}\(.*?(?=^    (?:async def|def|@staticmethod|@classmethod) )",
        re.MULTILINE | re.DOTALL,
    )
    updated, count = pattern.subn(replacement.rstrip() + "\n\n", source, count=1)
    if count != 1:
        raise RuntimeError(f"Could not replace service method {name}: {count}")
    return updated


def replace_module_function(source: str, name: str, replacement: str) -> str:
    pattern = re.compile(
        rf"^async def {re.escape(name)}\(.*?(?=^@mcp\.tool|^async def|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    updated, count = pattern.subn(replacement.rstrip() + "\n\n", source, count=1)
    if count != 1:
        raise RuntimeError(f"Could not replace MCP function {name}: {count}")
    return updated


RESPONSE_V2 = '''from __future__ import annotations

import json
from datetime import datetime
from difflib import get_close_matches
from typing import Any, Literal
from zoneinfo import ZoneInfo

DataStatus = Literal[
    "ok",
    "no_data_in_range",
    "history_disabled",
    "truncated_by_limit",
    "partially_available",
    "source_unavailable",
]

MISSING_NUMERIC_SENTINELS = {-(1 << 53)}


def sanitize_value(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in MISSING_NUMERIC_SENTINELS:
        return None
    if isinstance(value, list):
        return [sanitize_value(item) for item in value]
    if isinstance(value, dict):
        return {key: sanitize_value(item) for key, item in value.items()}
    return value


def epoch_ns_to_iso(value: Any, timezone: str) -> str | None:
    if value is None:
        return None
    try:
        ns = int(value)
        tz = ZoneInfo(timezone)
    except (TypeError, ValueError, OverflowError, KeyError) as exc:
        raise ValueError(
            f"Invalid epoch nanoseconds or timezone: {value!r}, {timezone!r}"
        ) from exc
    seconds, remainder = divmod(ns, 1_000_000_000)
    dt = datetime.fromtimestamp(seconds, tz=tz).replace(
        microsecond=remainder // 1_000
    )
    return dt.isoformat(timespec="milliseconds")


def add_iso_times(value: Any, timezone: str) -> Any:
    value = sanitize_value(value)
    if isinstance(value, list):
        return [add_iso_times(item, timezone) for item in value]
    if not isinstance(value, dict):
        return value
    result = {
        key: add_iso_times(item, timezone) for key, item in value.items()
    }
    for key in ("dt", "t", "mt"):
        if key in result and result[key] is not None:
            result[f"{key}_iso"] = epoch_ns_to_iso(result[key], timezone)
    return result


def compact_log(row: dict[str, Any], timezone: str) -> dict[str, Any]:
    normalized = add_iso_times(row, timezone)
    msg = str(normalized.get("msg", ""))
    lowered = msg.lower()
    event_type = "log"
    if "edit portfolio" in lowered:
        event_type = "param_change"
    elif "trading" in lowered and any(
        token in lowered for token in (" on", "enabled", "start")
    ):
        event_type = "trading_on"
    elif "trading" in lowered and any(
        token in lowered for token in (" off", "disabled", "stop")
    ):
        event_type = "trading_off"
    elif "watchdog" in lowered:
        event_type = "watchdog_stop"
    elif "timer" in lowered:
        event_type = "timer_fired"
    elif "order" in lowered and ("error" in lowered or "ошиб" in lowered):
        event_type = "order_error"

    details: dict[str, Any] = {}
    if event_type == "param_change":
        start = msg.find("{")
        snapshot = None
        if start >= 0:
            try:
                snapshot = json.loads(msg[start:])
            except (json.JSONDecodeError, TypeError):
                pass
        if isinstance(snapshot, dict):
            details["changed_fields"] = sorted(snapshot)[:100]
        details["diff_available"] = False
        details["note"] = (
            "Viking log contains a snapshot, not before/after values."
        )
    elif msg:
        details["message"] = msg[:500]
        if len(msg) > 500:
            details["message_truncated"] = True

    return {
        "event_type": event_type,
        "dt": normalized.get("dt"),
        "dt_iso": normalized.get("dt_iso"),
        "portfolio": normalized.get("name"),
        "robot_id": normalized.get("r_id"),
        "actor": normalized.get("owner") or "system",
        "level": normalized.get("level"),
        "details": details,
    }


def envelope(
    items: list[Any],
    *,
    data_status: DataStatus = "ok",
    truncated: bool = False,
    coverage: dict[str, Any] | None = None,
    notes: list[str] | None = None,
    **metadata: Any,
) -> dict[str, Any]:
    return {
        "data_status": data_status,
        "row_count": len(items),
        "truncated": truncated,
        "coverage": coverage,
        "items": items,
        "notes": notes or [],
        **metadata,
    }


def portfolio_not_found(
    portfolios: list[dict[str, Any]], robot_id: str, portfolio: str
) -> dict[str, Any] | None:
    robot_rows = [item for item in portfolios if item["robot_id"] == robot_id]
    if not robot_rows:
        return None
    names = [item["portfolio"] for item in robot_rows]
    if portfolio in names:
        return None
    return {
        "data_status": "source_unavailable",
        "error_type": "portfolio_not_found",
        "robot_id": robot_id,
        "portfolio": portfolio,
        "similar_portfolios": get_close_matches(
            portfolio, names, n=5, cutoff=0.35
        ),
    }
'''

LIST_METHOD = '''    async def list_available_portfolios(
        self, *, history_only: bool = False
    ) -> dict[str, Any]:
        all_portfolios = await self.client.list_portfolios()
        portfolios = (
            [item for item in all_portfolios if item["history_available"]]
            if history_only
            else all_portfolios
        )
        return envelope(
            portfolios,
            total_count=len(all_portfolios),
            returned_count=len(portfolios),
            history_only=history_only,
        )

    async def search_portfolios(
        self,
        *,
        query: str | None = None,
        robot_id: str | None = None,
        owner: str | None = None,
        history_only: bool = False,
        limit: int = 200,
        offset: int = 0,
    ) -> dict[str, Any]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be in range 1..1000")
        if offset < 0:
            raise ValueError("offset must be non-negative")
        rows = await self.client.list_portfolios()
        if query:
            needle = query.casefold().replace("*", "")
            rows = [
                item
                for item in rows
                if needle in item["portfolio"].casefold()
            ]
        if robot_id:
            rows = [item for item in rows if item["robot_id"] == robot_id]
        if owner:
            rows = [
                item
                for item in rows
                if owner.casefold() in item["owner"].casefold()
            ]
        if history_only:
            rows = [item for item in rows if item["history_available"]]
        total_count = len(rows)
        items = rows[offset : offset + limit]
        return envelope(
            items,
            truncated=offset + len(items) < total_count,
            total_count=total_count,
            returned_count=len(items),
            offset=offset,
            limit=limit,
        )'''

CURRENT_METHOD = '''    async def get_current_portfolio_data(
        self,
        *,
        robot_id: str,
        portfolio: str,
        raw: bool = False,
    ) -> dict[str, Any]:
        portfolios = await self.client.list_portfolios()
        not_found = portfolio_not_found(portfolios, robot_id, portfolio)
        if not_found is not None:
            return not_found
        result = await self.client.get_current_portfolio_data(
            robot_id=robot_id,
            portfolio=portfolio,
        )
        value = sanitize_value(result["value"])
        response = envelope(
            [value],
            robot_id=robot_id,
            portfolio=portfolio,
            subscription_closed=True,
        )
        if raw:
            response["raw_response"] = result
        return response'''

LOG_METHOD = '''    async def get_robot_log_history(
        self,
        *,
        robot_id: str,
        date_from: datetime,
        date_to: datetime,
        message_filter: str | None,
        limit: int,
        verbosity: str = "compact",
        timezone: str = "Europe/Moscow",
        raw: bool = False,
    ) -> dict[str, Any]:
        if verbosity not in {"compact", "full"}:
            raise ValueError("verbosity must be compact or full")
        mint_ns = self._to_epoch_ns(date_from, "date_from")
        maxt_ns = self._to_epoch_ns(date_to, "date_to")
        if int(mint_ns) >= int(maxt_ns):
            raise ValueError("date_from must be earlier than date_to")
        if message_filter is not None and len(message_filter) > 256:
            raise ValueError("message_filter must not exceed 256 characters")
        if not 1 <= limit <= 100_000:
            raise ValueError("limit must be in range 1..100000")
        result = await self.client.get_robot_log_history(
            robot_id=robot_id,
            mint_ns=mint_ns,
            maxt_ns=maxt_ns,
            message_filter=message_filter,
            limit=limit,
        )
        logs = result["logs"]
        items = (
            [compact_log(item, timezone) for item in logs]
            if verbosity == "compact"
            else [add_iso_times(item, timezone) for item in logs]
        )
        response = envelope(
            items,
            data_status="ok" if items else "no_data_in_range",
            truncated=len(logs) >= limit,
            coverage={
                "from": date_from.isoformat(),
                "to": date_to.isoformat(),
                "tz": timezone,
            },
            notes=[] if items else ["Логов в запрошенном диапазоне нет."],
            robot_id=robot_id,
            verbosity=verbosity,
        )
        if raw:
            response["raw_response"] = result
        return response'''

PREVIOUS_DEALS_METHOD = '''    async def get_previous_portfolio_deals(
        self,
        *,
        robot_id: str,
        portfolio: str,
        before: datetime,
        security_key: str | None,
        limit: int,
        timezone: str = "Europe/Moscow",
        raw: bool = False,
    ) -> dict[str, Any]:
        before_ns = self._to_epoch_ns(before, "before")
        result = await self.client.get_previous_portfolio_deals(
            robot_id=robot_id,
            portfolio=portfolio,
            before_ns=before_ns,
            security_key=security_key,
            limit=limit,
        )
        items = [add_iso_times(item, timezone) for item in result["deals"]]
        response = envelope(
            items,
            data_status="ok" if items else "no_data_in_range",
            truncated=len(items) >= limit,
            coverage={"to": before.isoformat(), "tz": timezone},
            robot_id=robot_id,
            portfolio=portfolio,
            security_key=security_key,
            estimated_price_share=(
                sum(1 for item in items if item.get("aggr") is True)
                / len(items)
                if items
                else 0.0
            ),
        )
        if raw:
            response["raw_response"] = result
        return response'''

DEAL_HISTORY_METHOD = '''    async def get_portfolio_deal_history(
        self,
        *,
        robot_id: str,
        portfolio: str,
        date_from: datetime,
        date_to: datetime,
        security_key: str | None,
        limit: int,
        timezone: str = "Europe/Moscow",
        raw: bool = False,
    ) -> dict[str, Any]:
        mint_ns = self._to_epoch_ns(date_from, "date_from")
        maxt_ns = self._to_epoch_ns(date_to, "date_to")
        if int(mint_ns) > int(maxt_ns):
            raise ValueError("date_from must not be later than date_to")
        result = await self.client.get_portfolio_deal_history(
            robot_id=robot_id,
            portfolio=portfolio,
            mint_ns=mint_ns,
            maxt_ns=maxt_ns,
            security_key=security_key,
            limit=limit,
        )
        items = [add_iso_times(item, timezone) for item in result["deals"]]
        notes: list[str] = []
        metadata: dict[str, Any] = {}
        status = "ok"
        if not items:
            status = "no_data_in_range"
            previous = await self.client.get_previous_portfolio_deals(
                robot_id=robot_id,
                portfolio=portfolio,
                before_ns=mint_ns,
                security_key=security_key,
                limit=1,
            )
            previous_items = [
                add_iso_times(item, timezone) for item in previous["deals"]
            ]
            if previous_items:
                nearest = previous_items[-1]
                metadata["nearest_earlier"] = nearest.get("dt_iso")
                notes.append(
                    "Сделок в запрошенном окне нет. "
                    f"Ближайшая более ранняя сделка: {nearest.get('dt_iso')}."
                )
            else:
                notes.append(
                    "Сделок в запрошенном окне и более ранних "
                    "доступных сделок нет."
                )
        response = envelope(
            items,
            data_status=status,
            truncated=len(items) >= limit,
            coverage={
                "from": date_from.isoformat(),
                "to": date_to.isoformat(),
                "tz": timezone,
            },
            notes=notes,
            robot_id=robot_id,
            portfolio=portfolio,
            security_key=security_key,
            estimated_price_share=(
                sum(1 for item in items if item.get("aggr") is True)
                / len(items)
                if items
                else 0.0
            ),
            **metadata,
        )
        if raw:
            response["raw_response"] = result
        return response'''


def patch_service() -> None:
    path = ROOT / "app/service.py"
    source = path.read_text(encoding="utf-8")
    import_line = "from app.export_store import ExportedFile, ExportStore\n"
    helper_import = (
        "from app.response_v2 import (\n"
        "    add_iso_times,\n"
        "    compact_log,\n"
        "    envelope,\n"
        "    portfolio_not_found,\n"
        "    sanitize_value,\n"
        ")\n"
    )
    if helper_import not in source:
        source = source.replace(import_line, import_line + helper_import)
    source = replace_class_method(source, "list_available_portfolios", LIST_METHOD)
    source = replace_class_method(source, "get_current_portfolio_data", CURRENT_METHOD)
    source = replace_class_method(source, "get_robot_log_history", LOG_METHOD)
    source = replace_class_method(
        source, "get_previous_portfolio_deals", PREVIOUS_DEALS_METHOD
    )
    source = replace_class_method(
        source, "get_portfolio_deal_history", DEAL_HISTORY_METHOD
    )

    marker = "        normalized_fields = self._validate_fields(fields or DEFAULT_FIELDS)\n"
    history_guard = '''        normalized_fields = self._validate_fields(fields or DEFAULT_FIELDS)
        portfolios = await self.client.list_portfolios()
        not_found = portfolio_not_found(portfolios, robot_id, portfolio)
        if not_found is not None:
            return DataDelivery(
                structured=not_found,
                summary="Портфель не найден.",
            )
        selected = next(
            item
            for item in portfolios
            if item["robot_id"] == robot_id
            and item["portfolio"] == portfolio
        )
        if not selected["history_available"]:
            structured = envelope(
                [],
                data_status="history_disabled",
                coverage={
                    "from": date_from.isoformat(),
                    "to": date_to.isoformat(),
                    "tz": str(date_from.tzinfo),
                },
                notes=["Сбор истории для портфеля отключён."],
                robot_id=robot_id,
                portfolio=portfolio,
            )
            return DataDelivery(
                structured=structured,
                summary="История для портфеля отключена.",
            )
'''
    if history_guard not in source:
        if marker not in source:
            raise RuntimeError("Could not find get_portfolio_data guard marker")
        source = source.replace(marker, history_guard, 1)
    path.write_text(source, encoding="utf-8")


def patch_main() -> None:
    path = ROOT / "app/main.py"
    source = path.read_text(encoding="utf-8")
    source = source.replace(
        "from typing import Annotated, Any",
        "from typing import Annotated, Any, Literal",
    )

    list_anchor = '''async def list_available_portfolios(history_only: bool = False) -> dict[str, Any]:
    return await _service_for_request().list_available_portfolios(history_only=history_only)
'''
    search_tool = '''

@mcp.tool(
    title="Поиск портфелей",
    description=(
        "Фильтрует доступные портфели на сервере по подстроке имени, "
        "robot_id, владельцу и history_available. Возвращает total_count "
        "и постраничный items."
    ),
    annotations=READ_ONLY,
)
async def search_portfolios(
    query: str | None = None,
    robot_id: str | None = None,
    owner: str | None = None,
    history_only: bool = False,
    limit: Annotated[int, Field(ge=1, le=1000)] = 200,
    offset: Annotated[int, Field(ge=0)] = 0,
) -> dict[str, Any]:
    return await _service_for_request().search_portfolios(
        query=query,
        robot_id=robot_id,
        owner=owner,
        history_only=history_only,
        limit=limit,
        offset=offset,
    )
'''
    if "async def search_portfolios(" not in source:
        source = source.replace(list_anchor, list_anchor + search_tool, 1)

    current = '''async def get_current_portfolio_data(
    robot_id: Annotated[str, Field(min_length=1, description="Идентификатор робота")],
    portfolio: Annotated[str, Field(min_length=1, description="Имя портфеля")],
    raw: bool = False,
) -> CallToolResult:
    try:
        result = await _service_for_request().get_current_portfolio_data(
            robot_id=robot_id,
            portfolio=portfolio,
            raw=raw,
        )
    except SUBSCRIPTION_ERRORS as exc:
        logger.warning("Current portfolio data request failed: %s", exc)
        return _error_result(exc)
    if result.get("error_type") == "portfolio_not_found":
        return CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text="Портфель не найден среди доступных портфелей робота.",
                )
            ],
            structuredContent=result,
            isError=True,
        )
    value = result["items"][0]
    securities = value.get("securities", {})
    return CallToolResult(
        content=[
            TextContent(
                type="text",
                text=(
                    f"Получен текущий снапшот {robot_id}/{portfolio}: "
                    f"{len(value)} полей, {len(securities)} инструментов."
                ),
            )
        ],
        structuredContent=result,
    )'''
    source = replace_module_function(source, "get_current_portfolio_data", current)

    log_history = '''async def get_robot_log_history(
    robot_id: Annotated[str, Field(min_length=1, description="Идентификатор робота")],
    date_from: Annotated[datetime, Field(description="Начало периода с часовым поясом")],
    date_to: Annotated[datetime, Field(description="Конец периода с часовым поясом")],
    message_filter: Annotated[
        str | None,
        Field(
            max_length=256,
            description="Маска msg: * — любое число символов, . — один символ",
        ),
    ] = None,
    limit: Annotated[int, Field(ge=1, le=100_000)] = 100_000,
    verbosity: Literal["compact", "full"] = "compact",
    timezone: str = "Europe/Moscow",
    raw: bool = False,
) -> CallToolResult:
    try:
        result = await _service_for_request().get_robot_log_history(
            robot_id=robot_id,
            date_from=date_from,
            date_to=date_to,
            message_filter=message_filter,
            limit=limit,
            verbosity=verbosity,
            timezone=timezone,
            raw=raw,
        )
    except SUBSCRIPTION_ERRORS as exc:
        logger.warning("Robot log history request failed: %s", exc)
        return _error_result(exc)
    return CallToolResult(
        content=[
            TextContent(
                type="text",
                text=(
                    "Получено записей истории логов робота: "
                    f"{result['row_count']}."
                ),
            )
        ],
        structuredContent=result,
    )'''
    source = replace_module_function(source, "get_robot_log_history", log_history)

    previous = '''async def get_previous_portfolio_deals(
    robot_id: Annotated[str, Field(min_length=1)],
    portfolio: Annotated[str, Field(min_length=1)],
    before: Annotated[datetime, Field(description="ISO 8601 с часовым поясом")],
    security_key: Annotated[str | None, Field(min_length=1)] = None,
    limit: Annotated[int, Field(ge=1, le=100)] = 100,
    timezone: str = "Europe/Moscow",
    raw: bool = False,
) -> CallToolResult:
    try:
        result = await _service_for_request().get_previous_portfolio_deals(
            robot_id=robot_id,
            portfolio=portfolio,
            before=before,
            security_key=security_key,
            limit=limit,
            timezone=timezone,
            raw=raw,
        )
    except SUBSCRIPTION_ERRORS as exc:
        logger.warning("Previous portfolio deals request failed: %s", exc)
        return _error_result(exc)
    return CallToolResult(
        content=[
            TextContent(
                type="text",
                text=f"Получено сделок: {result['row_count']}.",
            )
        ],
        structuredContent=result,
    )'''
    source = replace_module_function(
        source, "get_previous_portfolio_deals", previous
    )

    history = '''async def get_portfolio_deal_history(
    robot_id: Annotated[str, Field(min_length=1)],
    portfolio: Annotated[str, Field(min_length=1)],
    date_from: Annotated[datetime, Field(description="ISO 8601 с часовым поясом")],
    date_to: Annotated[datetime, Field(description="ISO 8601 с часовым поясом")],
    security_key: Annotated[str | None, Field(min_length=1)] = None,
    limit: Annotated[int, Field(ge=1, le=100_000)] = 100_000,
    timezone: str = "Europe/Moscow",
    raw: bool = False,
) -> CallToolResult:
    try:
        result = await _service_for_request().get_portfolio_deal_history(
            robot_id=robot_id,
            portfolio=portfolio,
            date_from=date_from,
            date_to=date_to,
            security_key=security_key,
            limit=limit,
            timezone=timezone,
            raw=raw,
        )
    except SUBSCRIPTION_ERRORS as exc:
        logger.warning("Portfolio deal history request failed: %s", exc)
        return _error_result(exc)
    return CallToolResult(
        content=[
            TextContent(
                type="text",
                text=f"Получено сделок: {result['row_count']}.",
            )
        ],
        structuredContent=result,
    )'''
    source = replace_module_function(source, "get_portfolio_deal_history", history)
    path.write_text(source, encoding="utf-8")


def patch_tests() -> None:
    response_test = '''from app.response_v2 import add_iso_times, compact_log, envelope, sanitize_value


def test_sanitize_viking_missing_value_recursively():
    assert sanitize_value(
        {"orig_price": -(1 << 53), "nested": [1, -(1 << 53)]}
    ) == {"orig_price": None, "nested": [1, None]}


def test_add_iso_times_keeps_ns_and_adds_iso():
    row = add_iso_times(
        {"dt": "1774551550351384574"}, "Europe/Moscow"
    )
    assert row["dt"] == "1774551550351384574"
    assert row["dt_iso"] == "2026-03-26T21:59:10.351+03:00"


def test_compact_log_drops_large_snapshot_message():
    row = compact_log(
        {
            "dt": "1774551550351384574",
            "level": 1,
            "name": "alpha",
            "owner": "user@example.com",
            "msg": (
                'Edit portfolio {"lim_b": 1.5, '
                '"securities": {"A": {"pos": 1}}}'
            ),
        },
        "Europe/Moscow",
    )
    assert row["event_type"] == "param_change"
    assert "msg" not in row
    assert row["details"]["diff_available"] is False


def test_envelope_has_one_canonical_array():
    result = envelope([{"id": 1}], total_count=1)
    assert result["items"] == [{"id": 1}]
    assert result["row_count"] == 1
    assert "data" not in result
'''
    (ROOT / "tests/test_response_v2.py").write_text(
        response_test, encoding="utf-8"
    )

    path = ROOT / "tests/test_service.py"
    source = path.read_text(encoding="utf-8")
    source = source.replace('result["count"] == 1', 'result["returned_count"] == 1')
    source = source.replace('result["portfolios"][0]', 'result["items"][0]')
    source = source.replace(
        'result["value"]["custom_field"] == 42',
        'result["items"][0]["custom_field"] == 42',
    )
    source = source.replace(
        'result["unsubscribed"] is True',
        'result["subscription_closed"] is True',
    )
    source = source.replace(
        'assert result["mint"] == "1767225600000000000"\n'
        '    assert result["maxt"] == "1767225601000000000"\n'
        '    assert result["message_filter"] == "*test*"\n'
        '    assert result["date_from"] == "2026-01-01T00:00:00+00:00"\n'
        '    assert result["date_to"] == "2026-01-01T00:00:01+00:00"',
        'assert result["row_count"] == 1\n'
        '    assert result["coverage"]["from"] == "2026-01-01T00:00:00+00:00"\n'
        '    assert result["coverage"]["to"] == "2026-01-01T00:00:01+00:00"',
    )
    path.write_text(source, encoding="utf-8")


def patch_docs() -> None:
    readme_path = ROOT / "README.md"
    readme = readme_path.read_text(encoding="utf-8")
    section = '''

## Response contract v2

Исторические сделки, логи и текущий снапшот возвращаются без дублирования в едином массиве `items`. Сырой ответ Viking доступен только при `raw=true`. Ответ содержит `data_status`, `row_count`, `truncated`, `coverage` и `notes`; epoch-nanoseconds сохраняются, рядом добавляется `dt_iso`. Sentinel `-2^53` преобразуется в `null`. Логи по умолчанию выдаются с `verbosity=compact`; `full` нужно запрашивать явно.
'''
    if "## Response contract v2" not in readme:
        readme_path.write_text(readme + section, encoding="utf-8")

    agents_path = ROOT / "AGENTS.md"
    agents = agents_path.read_text(encoding="utf-8")
    section = '''

## Response contract v2

- Никогда не возвращать один массив одновременно внутри сырого `data.values` и в нормализованном поле. Канонический массив — `items`; raw допускается только по `raw=true`.
- Пустой результат обязан иметь `data_status` и человекочитаемую причину в `notes`.
- Viking sentinel `-9007199254740992` трактуется как отсутствие значения и возвращается как `null`. Не считать `2147483647` sentinel без подтверждения документацией: `d_pg` документирован как pagination data.
- Для epoch-nanoseconds сохранять исходное поле и добавлять `_iso` в запрошенной timezone.
- Полные JSON-снапшоты в `msg` не выдавать при compact-режиме. Не выдумывать before/after: если Viking пишет только снапшот, явно указывать `diff_available=false`.
- Новые агрегаты по роботу нельзя заявлять как быстрые, пока нет измеренного серверного кэша/параллельного плана и приёмочного теста.
'''
    if "## Response contract v2" not in agents:
        agents_path.write_text(agents + section, encoding="utf-8")


def main() -> None:
    (ROOT / "app/response_v2.py").write_text(RESPONSE_V2, encoding="utf-8")
    patch_service()
    patch_main()
    patch_tests()
    patch_docs()


if __name__ == "__main__":
    main()
