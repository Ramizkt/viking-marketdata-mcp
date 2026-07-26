from __future__ import annotations

import hashlib
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from starlette.datastructures import Headers

VIKING_EMAIL_HEADER = "X-Viking-Email"
VIKING_API_KEY_HEADER = "X-Viking-API-Key"
VIKING_ROLE_HEADER = "X-Viking-Role"
REQUIRED_HEADERS = (VIKING_EMAIL_HEADER, VIKING_API_KEY_HEADER, VIKING_ROLE_HEADER)


@dataclass(frozen=True)
class VikingCredentials:
    email: str
    api_key: str
    role: str

    @property
    def fingerprint(self) -> str:
        value = f"{self.email}\0{self.role}\0{self.api_key}".encode()
        return hashlib.sha256(value).hexdigest()


_request_credentials: ContextVar[VikingCredentials | None] = ContextVar(
    "viking_request_credentials",
    default=None,
)


def credentials_from_scope(scope: dict[str, Any]) -> tuple[VikingCredentials | None, list[str]]:
    headers = Headers(scope=scope)
    values = {
        VIKING_EMAIL_HEADER: headers.get(VIKING_EMAIL_HEADER, "").strip(),
        VIKING_API_KEY_HEADER: headers.get(VIKING_API_KEY_HEADER, "").strip(),
        VIKING_ROLE_HEADER: headers.get(VIKING_ROLE_HEADER, "").strip(),
    }
    missing = [name for name in REQUIRED_HEADERS if not values[name]]
    if missing:
        return None, missing
    return (
        VikingCredentials(
            email=values[VIKING_EMAIL_HEADER],
            api_key=values[VIKING_API_KEY_HEADER],
            role=values[VIKING_ROLE_HEADER].lower(),
        ),
        [],
    )


def set_request_credentials(credentials: VikingCredentials):
    return _request_credentials.set(credentials)


def reset_request_credentials(token) -> None:
    _request_credentials.reset(token)


def require_request_credentials() -> VikingCredentials:
    credentials = _request_credentials.get()
    if credentials is None:
        raise RuntimeError(
            "Viking credentials are missing. Configure X-Viking-Email, "
            "X-Viking-API-Key and X-Viking-Role HTTP headers in the MCP client."
        )
    return credentials
