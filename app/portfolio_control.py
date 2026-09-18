"""Allowlisted portfolio writes; acknowledgements are not trading-state verification."""

from __future__ import annotations

import asyncio
import contextlib
import math
import re
from time import monotonic
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt, field_validator, model_validator

from app.viking_client import VikingAPIError, VikingClient, VikingProtocolError

MAX_TARGETS = 200
SAFE_NUMBER = (1 << 53) - 1
Action = Literal["user_fields", "stop", "hard_stop", "stop_formulas"]
Side = Literal["both", "sell", "buy"]
UF_KEY = re.compile(r"uf(?:[0-9]|1[0-9])\Z")


class PortfolioTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    robot_id: str = Field(min_length=1, max_length=128)
    portfolio: str = Field(min_length=1, max_length=256)

    @field_validator("robot_id", "portfolio")
    @classmethod
    def exact_identity(cls, value: str) -> str:
        if value != value.strip() or any(c in value for c in "*?\x00\n\r"):
            raise ValueError("Use an exact non-blank identity, without wildcards or surrounding whitespace")
        return value


class UserFieldPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    v: StrictInt | StrictFloat | None = None
    c: str | None = Field(default=None, max_length=4096)

    @model_validator(mode="after")
    def nonempty_partial_patch(self) -> UserFieldPatch:
        if not self.model_fields_set or any(getattr(self, k) is None for k in self.model_fields_set):
            raise ValueError("A user field needs v and/or c; null is not a supported write value")
        return self


UserFields = Annotated[
    dict[Annotated[str, Field(pattern=r"^uf(?:[0-9]|1[0-9])$")], StrictInt | StrictFloat | UserFieldPatch],
    Field(min_length=1, max_length=20),
]


def normalize_user_fields(fields: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(fields, dict) or not 1 <= len(fields) <= 20:
        raise ValueError("fields must contain 1..20 user fields")
    result = {}
    for key, value in fields.items():
        if not isinstance(key, str) or UF_KEY.fullmatch(key) is None:
            raise ValueError("Only uf0..uf19 can be changed")
        if isinstance(value, UserFieldPatch):
            patch = value.model_dump(exclude_unset=True)
        elif isinstance(value, dict):
            patch = UserFieldPatch.model_validate(value).model_dump(exclude_unset=True)
        else:
            patch = {"v": value}
        if "v" in patch:
            number = patch["v"]
            if type(number) not in (int, float) or not -SAFE_NUMBER <= number <= SAFE_NUMBER:
                raise ValueError(f"{key}.v must be a finite number in the JSON-safe range")
        if "c" in patch:
            try:
                patch["c"].encode("utf-8")
            except UnicodeError as exc:
                raise ValueError(f"{key}.c must be valid UTF-8") from exc
        result[key] = patch
    return result


def validate_user_field_template(fields: dict[str, dict[str, Any]], resolved: dict[str, Any]) -> None:
    """Fail closed on stale/malformed/non-editable target templates; never merge a snapshot into a patch."""
    template = resolved.get("template")
    if not isinstance(template, dict) or not isinstance(resolved.get("template_id"), str):
        raise VikingProtocolError("Missing portfolio template identity")
    if template.get("template_id") != resolved["template_id"]:
        raise VikingProtocolError(
            "Returned portfolio template identity does not match the requested template"
        )
    sections = resolved.get("template_fields")
    entries = sections.get("portfolio") if isinstance(sections, dict) else None
    if not isinstance(entries, list):
        raise VikingProtocolError("Portfolio template fields must be an array")
    for key, patch in fields.items():
        matches = [item for item in entries if isinstance(item, dict) and item.get("field") == key]
        if len(matches) != 1:
            raise VikingProtocolError(f"Template must describe {key} exactly once")
        spec = matches[0]
        if spec.get("disabled") is not False or spec.get("editor") != "user_value":
            raise ValueError(f"{key} is not editable in the current portfolio template")
        if "v" in patch:
            low, high = spec.get("min"), spec.get("max")
            if (
                any(
                    type(x) not in (int, float) or (isinstance(x, float) and not math.isfinite(x))
                    for x in (low, high)
                )
                or low > high
            ):
                raise VikingProtocolError(f"Template has invalid numeric bounds for {key}")
            if not low <= patch["v"] <= high:
                raise ValueError(f"{key}.v must be in the template range {low}..{high}")
        if "c" in patch:
            limit = spec.get("max_len")
            if type(limit) is not int or limit < 0:
                raise VikingProtocolError(f"Template has invalid caption length for {key}")
            if len(patch["c"].encode("utf-8")) > limit:
                raise ValueError(f"{key}.c exceeds the template limit of {limit} UTF-8 bytes")


def validate_controls(dry_run: bool, confirm: bool) -> None:
    if type(dry_run) is not bool or type(confirm) is not bool:
        raise ValueError("dry_run and confirm must be JSON booleans")
    if not dry_run and not confirm:
        raise ValueError(
            "Execution requires dry_run=false and confirm=true for the exact requested operation"
        )


def command_for(
    target: PortfolioTarget, action: Action, fields: Any = None, side: Side = "both"
) -> tuple[str, dict[str, Any]]:
    if side not in ("both", "sell", "buy"):
        raise ValueError("side must be both, sell or buy")
    if action == "user_fields":
        return "portfolio.update", {
            "r_id": target.robot_id,
            "portfolio": {"name": target.portfolio, **normalize_user_fields(fields)},
        }
    if fields is not None:
        raise ValueError("Stop operations do not accept arbitrary fields")
    if action == "stop":
        flags = {f"re_{s}": False for s in ("sell", "buy") if side in ("both", s)}
        return "portfolio.update", {"r_id": target.robot_id, "portfolio": {"name": target.portfolio, **flags}}
    types = {"hard_stop": "portfolio.hard_stop", "stop_formulas": "portfolio.formulas_stop"}
    if action not in types:
        raise ValueError("Unsupported portfolio control operation")
    if side != "both":
        raise ValueError("Hard stop and Stop formulas apply to both sides")
    return types[action], {"r_id": target.robot_id, "p_id": target.portfolio}


class WriteOutcomeUnknown(RuntimeError):
    """The request may have executed. Never replay it automatically."""

    def __init__(self, cause: Exception, response: dict[str, Any] | None = None):
        super().__init__("Write outcome is unknown; inspect current state before deciding whether to retry")
        self.cause = cause
        self.response = response


class WriteNotSent(RuntimeError):
    def __init__(self, cause: Exception):
        super().__init__("Could not establish an authorized connection; portfolio command was not sent")
        self.cause = cause


async def send_control_once(
    client: VikingClient,
    *,
    robot_id: str,
    portfolio: str,
    action: Action,
    fields: Any = None,
    side: Side = "both",
) -> dict[str, Any]:
    """Separate transport from read retry: one send, strict acknowledgement, no automatic replay."""
    target = PortfolioTarget(robot_id=robot_id, portfolio=portfolio)
    message_type, data = command_for(target, action, fields, side)
    # Shared per-credential lock also bounds bulk writes to at most ten sends/second.
    async with client._portfolio_control_lock:
        await asyncio.sleep(max(0, client._portfolio_control_next_send_at - monotonic()))
        try:
            await client._ensure_connected()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise WriteNotSent(exc) from exc
        client._portfolio_control_next_send_at = monotonic() + 0.1
        response = None
        try:
            response = await client._request_connected(
                message_type, data, timeout=client.settings.viking_request_timeout_seconds
            )
            ack = client._parse_plain_response(response, message_type)
            if (
                client._required_str(ack["data"], "r_id") != robot_id
                or client._required_str(ack["data"], "p_id") != portfolio
            ):
                raise VikingProtocolError("Portfolio write acknowledgement has a different target")
        except VikingAPIError:
            raise
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await client.close()
            raise
        except Exception as exc:
            with contextlib.suppress(Exception):
                await client.close()
            raise WriteOutcomeUnknown(exc, response) from exc
        return response


NOTES = {
    "user_fields": [
        "Only supplied uf0..uf19 v/c subkeys are patched. User fields can affect trading formulas.",
        "Read back fields with get_current_portfolio_data; acknowledgement is not readback.",
    ],
    "stop": [
        "Only the selected re_sell/re_buy flags are set to false; no flags are enabled.",
        "Stop portfolios leaves second-leg orders working; SL/Timer can reprice them.",
        "Timetable or formulas may override these flags. This is not Hard stop or Stop formulas.",
    ],
    "hard_stop": [
        "Hard stop disables both re flags and timetable, and attempts cancellation on both legs.",
        "Formulas can re-enable trading. Stop formulas requires a separate explicit user instruction.",
    ],
    "stop_formulas": [
        "Stop formulas disables formulas and changes formula-driven modes to constants/Standard.",
        "Re-enabling formulas later is manual. This command attempts cancellation on both legs.",
    ],
}


def _error_details(exc: Exception) -> dict[str, Any]:
    cause = exc.cause if isinstance(exc, (WriteOutcomeUnknown, WriteNotSent)) else exc
    details: dict[str, Any] = {"error_type": type(cause).__name__, "message": str(exc)}
    if isinstance(cause, VikingAPIError):
        details.update(code=cause.code, api_message=str(cause), api_response=cause.response)
    elif isinstance(exc, WriteOutcomeUnknown) and exc.response is not None:
        details["api_response"] = exc.response
    return details


class PortfolioControlService:
    def __init__(self, client: VikingClient):
        self.client = client

    async def run(
        self,
        *,
        targets: list[PortfolioTarget | dict[str, str]],
        action: Action,
        fields: Any = None,
        side: Side = "both",
        dry_run: bool = True,
        confirm: bool = False,
    ) -> dict[str, Any]:
        validate_controls(dry_run, confirm)
        if not isinstance(targets, list) or not 1 <= len(targets) <= MAX_TARGETS:
            raise ValueError(f"Provide an explicit list of 1..{MAX_TARGETS} portfolio targets")
        normalized = [PortfolioTarget.model_validate(t) for t in targets]
        identities = {(t.robot_id, t.portfolio) for t in normalized}
        if len(identities) != len(normalized):
            raise ValueError("Duplicate portfolio targets are not allowed")
        # Validate the entire batch locally before the first write.
        commands = [command_for(t, action, fields, side) for t in normalized]
        if action == "user_fields":
            if len(normalized) != 1:
                raise ValueError("User fields are changed for one explicitly selected portfolio per call")
            fields = normalize_user_fields(fields)
            resolved = await self.client.get_portfolio_template(**normalized[0].model_dump())
            validate_user_field_template(fields, resolved)
        items = []
        interrupted = False
        for target, (message_type, data) in zip(normalized, commands, strict=True):
            item: dict[str, Any] = {**target.model_dump(), "operation": action, "verified": False}
            if dry_run:
                item.update(status="preview", request={"type": message_type, "data": data})
            elif interrupted:
                item.update(
                    status="not_sent", reason="Batch interrupted after a transport failure; not retried"
                )
            else:
                try:
                    ack = await self.client.execute_portfolio_control(
                        **target.model_dump(), action=action, fields=fields, side=side
                    )
                    item.update(status="accepted", acknowledgement=ack)
                except VikingAPIError as exc:
                    item.update(status="rejected", error=_error_details(exc))
                except WriteOutcomeUnknown as exc:
                    item.update(status="outcome_unknown", error=_error_details(exc))
                    interrupted = True
                except WriteNotSent as exc:
                    item.update(status="not_sent", error=_error_details(exc))
                    interrupted = True
            items.append(item)
        statuses = ("preview", "accepted", "rejected", "outcome_unknown", "not_sent")
        counts = {status: sum(i["status"] == status for i in items) for status in statuses}
        has_errors = any(counts[s] for s in ("rejected", "outcome_unknown", "not_sent"))
        return {
            "status": "preview" if dry_run else ("partial_failure" if has_errors else "accepted"),
            "data_status": "present",
            "items": items,
            "counts": counts,
            "has_errors": has_errors,
            "dry_run": dry_run,
            "atomic": False,
            "automatic_retry": False,
            "notes": [
                *NOTES[action],
                "Accepted means API acknowledgement, not verified trading state or exchange cancellation.",
                "Verify trading via get_robot_portfolio_trading_status and active orders via subscriptions.",
                "Stops do not send position-closing commands. No automatic rollback or replay is performed.",
                "Preview is not a reservation: permissions, templates and state may change before execution.",
            ],
        }
