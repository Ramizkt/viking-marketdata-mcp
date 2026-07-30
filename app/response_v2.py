from __future__ import annotations

import contextlib
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
            with contextlib.suppress(json.JSONDecodeError, TypeError):
                snapshot = json.loads(msg[start:])
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
