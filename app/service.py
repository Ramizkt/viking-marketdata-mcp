from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from app.config import Settings
from app.export_store import ExportedFile, ExportStore
from app.viking_client import VikingClient

Aggregation = Literal["raw", "10s", "1m", "5m", "10m", "1h", "6h", "24h"]
Delivery = Literal["auto", "inline", "file", "stream", "summary"]

DEFAULT_FIELDS = ["buy", "sell", "pos"]
SUPPORTED_FIELDS = {
    "sell",
    "buy",
    "lim_s",
    "lim_b",
    "price_s",
    "price_b",
    "pos",
    "fin_res",
    *(f"uf{index}" for index in range(20)),
}
MISSING_VALUE = -(1 << 53)


@dataclass(frozen=True)
class DataDelivery:
    structured: dict[str, Any]
    summary: str
    exported_file: ExportedFile | None = None


class MarketDataService:
    def __init__(self, settings: Settings, client: VikingClient, export_store: ExportStore):
        self.settings = settings
        self.client = client
        self.export_store = export_store

    async def list_available_portfolios(self, *, history_only: bool = False) -> dict[str, Any]:
        portfolios = await self.client.list_portfolios()
        if history_only:
            portfolios = [item for item in portfolios if item["history_available"]]
        return {
            "count": len(portfolios),
            "history_only": history_only,
            "portfolios": portfolios,
        }

    async def subscribe_available_portfolios(self) -> dict[str, Any]:
        return await self.client.subscribe_available_portfolios()

    async def get_available_portfolio_updates(
        self, *, subscription_id: str, wait_seconds: float, max_events: int
    ) -> dict[str, Any]:
        return await self.client.get_available_portfolio_updates(
            subscription_id,
            wait_seconds=wait_seconds,
            max_events=max_events,
        )

    async def unsubscribe_available_portfolios(self, *, subscription_id: str) -> dict[str, Any]:
        return await self.client.unsubscribe_available_portfolios(subscription_id)

    async def get_current_portfolio_data(
        self,
        *,
        robot_id: str,
        portfolio: str,
    ) -> dict[str, Any]:
        return await self.client.get_current_portfolio_data(
            robot_id=robot_id,
            portfolio=portfolio,
        )

    async def subscribe_portfolio(
        self,
        *,
        robot_id: str,
        portfolio: str,
    ) -> dict[str, Any]:
        return await self.client.subscribe_portfolio(
            robot_id=robot_id,
            portfolio=portfolio,
        )

    async def get_portfolio_updates(
        self,
        *,
        subscription_id: str,
        wait_seconds: float,
        max_events: int,
    ) -> dict[str, Any]:
        return await self.client.get_portfolio_updates(
            subscription_id,
            wait_seconds=wait_seconds,
            max_events=max_events,
        )

    async def unsubscribe_portfolio(self, *, subscription_id: str) -> dict[str, Any]:
        return await self.client.unsubscribe_portfolio(subscription_id)

    async def get_portfolio_data(
        self,
        *,
        robot_id: str,
        portfolio: str,
        date_from: datetime,
        date_to: datetime,
        fields: list[str] | None,
        aggregation: Aggregation,
        delivery: Delivery,
        preview_rows: int,
    ) -> DataDelivery:
        normalized_fields = self._validate_fields(fields or DEFAULT_FIELDS)
        start_ms = self._to_epoch_ms(date_from, "date_from")
        end_ms = self._to_epoch_ms(date_to, "date_to")
        if start_ms >= end_ms:
            raise ValueError("date_from must be earlier than date_to")
        if preview_rows < 0 or preview_rows > 100:
            raise ValueError("preview_rows must be in range 0..100")

        history_by_field: dict[str, list[dict[str, Any]]] = {}
        for field in normalized_fields:
            history_by_field[field] = await self.client.get_portfolio_history(
                robot_id=robot_id,
                portfolio=portfolio,
                key=field,
                aggregation=aggregation,
                start_ms=start_ms,
                end_ms=end_ms,
                max_points=self.settings.max_points_per_field,
            )

        rows = self._merge_fields(history_by_field, normalized_fields)
        estimated_bytes = len(
            json.dumps(rows, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
        )
        actual_delivery, override_reason = self._choose_delivery(
            requested=delivery,
            row_count=len(rows),
            serialized_bytes=estimated_bytes,
        )

        base = {
            "robot_id": robot_id,
            "portfolio": portfolio,
            "date_from": date_from.astimezone(UTC).isoformat(),
            "date_to": date_to.astimezone(UTC).isoformat(),
            "fields": normalized_fields,
            "aggregation": aggregation,
            "row_count": len(rows),
            "estimated_json_bytes": estimated_bytes,
            "requested_delivery": delivery,
            "actual_delivery": actual_delivery,
            "overridden": override_reason is not None,
            "override_reason": override_reason,
            "preview": rows[:preview_rows],
        }

        if actual_delivery in {"inline", "summary"}:
            if actual_delivery == "inline":
                base["rows"] = rows
            summary = (
                f"Получено {len(rows)} строк для {robot_id}/{portfolio}; режим выдачи: {actual_delivery}."
            )
            return DataDelivery(structured=base, summary=summary)

        exported = self.export_store.save_csv(
            rows=rows,
            fields=normalized_fields,
            robot_id=robot_id,
            portfolio=portfolio,
        )
        base["file"] = {
            "name": exported.filename,
            "mime_type": "text/csv",
            "size_bytes": exported.size_bytes,
            "download_url": exported.download_url,
            "expires_at": datetime.fromtimestamp(exported.expires_at, tz=UTC).isoformat(),
        }
        summary = (
            f"Получено {len(rows)} строк для {robot_id}/{portfolio}. "
            f"Данные подготовлены как CSV-файл размером {exported.size_bytes} байт."
        )
        if override_reason:
            summary += f" Запрошенный режим был изменён: {override_reason}"
        return DataDelivery(structured=base, summary=summary, exported_file=exported)

    def _choose_delivery(
        self,
        *,
        requested: Delivery,
        row_count: int,
        serialized_bytes: int,
    ) -> tuple[Literal["inline", "file", "summary"], str | None]:
        within_inline_limit = (
            row_count <= self.settings.inline_max_rows and serialized_bytes <= self.settings.inline_max_bytes
        )
        if requested == "summary":
            return "summary", None
        if requested == "file":
            return "file", None
        if requested == "stream":
            return "file", "stream не реализован в MVP; результат возвращён файлом."
        if requested == "inline" and not within_inline_limit:
            return (
                "file",
                "inline-ответ превысил серверный лимит по числу строк или размеру.",
            )
        if requested == "inline":
            return "inline", None
        return ("inline", None) if within_inline_limit else ("file", None)

    @staticmethod
    def _merge_fields(
        history_by_field: dict[str, list[dict[str, Any]]],
        fields: list[str],
    ) -> list[dict[str, Any]]:
        changes: dict[int, dict[str, Any]] = {}
        for field, points in history_by_field.items():
            for point in points:
                changes.setdefault(int(point["dt"]), {})[field] = point["v"]

        current: dict[str, Any] = {}
        rows: list[dict[str, Any]] = []
        for timestamp in sorted(changes):
            for field, value in changes[timestamp].items():
                if value == MISSING_VALUE:
                    current.pop(field, None)
                else:
                    current[field] = value
            if all(field in current for field in fields):
                rows.append({"timestamp": timestamp, **{field: current[field] for field in fields}})
        return rows

    @staticmethod
    def _validate_fields(fields: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(field.strip() for field in fields if field.strip()))
        if not normalized:
            raise ValueError("At least one portfolio field is required")
        unsupported = sorted(set(normalized) - SUPPORTED_FIELDS)
        if unsupported:
            raise ValueError(
                "Unsupported portfolio fields: "
                + ", ".join(unsupported)
                + ". Supported: sell, buy, lim_s, lim_b, price_s, price_b, pos, fin_res, uf0..uf19."
            )
        return normalized

    @staticmethod
    def _to_epoch_ms(value: datetime, field_name: str) -> int:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{field_name} must include a timezone offset, for example +03:00 or Z")
        return int(value.timestamp() * 1000)
