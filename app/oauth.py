from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlparse
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import InvalidRedirectUriError, OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse

from app.config import Settings
from app.credentials import VikingCredentials
from app.viking_client import VikingClient

logger = logging.getLogger(__name__)

CredentialMode = Literal["session", "local"]
OAUTH_SCOPE = "viking.read"
TOKEN_AAD = b"viking-marketdata-mcp-token-v1"


class VikingAuthorizationCode(AuthorizationCode):
    credentials: VikingCredentials
    mode: CredentialMode


@dataclass
class PendingAuthorization:
    client_id: str
    params: AuthorizationParams
    expires_at: float
    recovered_client: OAuthClientInformationFull | None = None


@dataclass
class SessionToken:
    access: AccessToken
    credentials: VikingCredentials
    last_used: float


class RecoverableOAuthClient(OAuthClientInformationFull):
    """Temporary public client used to migrate registrations lost before persistence."""

    def validate_redirect_uri(self, redirect_uri: AnyUrl | None) -> AnyUrl:
        if redirect_uri is None:
            raise InvalidRedirectUriError("redirect_uri is required while recovering an OAuth client")
        parsed = urlparse(str(redirect_uri))
        is_loopback = parsed.hostname in {"127.0.0.1", "::1", "localhost"}
        if parsed.scheme != "https" and not (parsed.scheme == "http" and is_loopback):
            raise InvalidRedirectUriError(
                "Recovered OAuth clients must use HTTPS or a loopback HTTP redirect URI"
            )
        return redirect_uri


class VikingOAuthProvider(
    OAuthAuthorizationServerProvider[
        VikingAuthorizationCode,
        RefreshToken,
        AccessToken,
    ]
):
    """OAuth provider that keeps session credentials in RAM or in an encrypted client token."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.base_url = settings.resolved_public_base_url
        self._client_store_path = settings.resolved_oauth_client_store_path
        self._clients = self._load_clients()
        self._clients_lock = asyncio.Lock()
        self._pending: dict[str, PendingAuthorization] = {}
        self._codes: dict[str, VikingAuthorizationCode] = {}
        self._session_tokens: dict[str, SessionToken] = {}
        self._cleanup_task: asyncio.Task[None] | None = None

        secret = settings.credential_token_key or settings.export_signing_key
        if not secret:
            secret = secrets.token_urlsafe(48)
            logger.warning(
                "CREDENTIAL_TOKEN_KEY and EXPORT_SIGNING_KEY are empty; "
                "remembered OAuth tokens will stop working after restart"
            )
        key = hashlib.sha256(b"viking-oauth-token-key-v1\0" + secret.encode()).digest()
        self._cipher = AESGCM(key)

    def start(self) -> None:
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def close(self) -> None:
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            await asyncio.gather(self._cleanup_task, return_exceptions=True)
            self._cleanup_task = None
        self._pending.clear()
        self._codes.clear()
        self._session_tokens.clear()

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        client = self._clients.get(client_id)
        if client is not None:
            return client
        if not self._is_legacy_client_id(client_id):
            return None
        return RecoverableOAuthClient(
            client_id=client_id,
            client_secret=None,
            redirect_uris=[AnyUrl("http://127.0.0.1")],
            token_endpoint_auth_method="none",
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            scope=OAUTH_SCOPE,
        )

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if not client_info.client_id:
            raise RegistrationError("invalid_client_metadata", "client_id is required")
        for redirect_uri in client_info.redirect_uris or []:
            parsed = urlparse(str(redirect_uri))
            is_loopback = parsed.hostname in {"127.0.0.1", "::1", "localhost"}
            if parsed.scheme != "https" and not (parsed.scheme == "http" and is_loopback):
                raise RegistrationError(
                    "invalid_redirect_uri",
                    "Redirect URI must use HTTPS or a loopback HTTP address",
                )
        await self._persist_client(client_info)

    async def authorize(
        self,
        client: OAuthClientInformationFull,
        params: AuthorizationParams,
    ) -> str:
        if not client.client_id:
            raise AuthorizeError("invalid_request", "client_id is required")
        recovered_client = None
        if isinstance(client, RecoverableOAuthClient):
            recovered_client = OAuthClientInformationFull(
                client_id=client.client_id,
                client_secret=None,
                client_id_issued_at=int(time.time()),
                redirect_uris=[params.redirect_uri],
                token_endpoint_auth_method="none",
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
                scope=" ".join(params.scopes or [OAUTH_SCOPE]),
                client_name="Recovered MCP client",
            )
        pending_id = secrets.token_urlsafe(32)
        self._pending[pending_id] = PendingAuthorization(
            client_id=client.client_id,
            params=params,
            expires_at=time.time() + 600,
            recovered_client=recovered_client,
        )
        return f"{self.base_url}/oauth/connect/{pending_id}"

    async def load_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: str,
    ) -> VikingAuthorizationCode | None:
        code = self._codes.get(authorization_code)
        if code is None or code.client_id != client.client_id:
            return None
        return code

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: VikingAuthorizationCode,
    ) -> OAuthToken:
        stored = self._codes.pop(authorization_code.code, None)
        if stored is None or stored.client_id != client.client_id:
            raise TokenError("invalid_grant", "authorization code was already used")

        scopes = stored.scopes or [OAUTH_SCOPE]
        now = int(time.time())
        subject = self._subject(stored.credentials)

        if stored.mode == "session":
            ttl = self.settings.oauth_session_max_ttl_seconds
            token = f"s1_{secrets.token_urlsafe(36)}"
            access = AccessToken(
                token=token,
                client_id=stored.client_id,
                scopes=scopes,
                expires_at=now + ttl,
                resource=stored.resource,
                subject=subject,
                claims={"iss": self.base_url, "credential_mode": "session"},
            )
            self._session_tokens[token] = SessionToken(
                access=access,
                credentials=stored.credentials,
                last_used=time.monotonic(),
            )
        else:
            ttl = self.settings.oauth_persistent_token_ttl_seconds
            token = self._encrypt_token(
                credentials=stored.credentials,
                client_id=stored.client_id,
                scopes=scopes,
                resource=stored.resource,
                subject=subject,
                expires_at=now + ttl,
            )

        return OAuthToken(
            access_token=token,
            token_type="Bearer",
            expires_in=ttl,
            scope=" ".join(scopes),
        )

    async def load_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: str,
    ) -> RefreshToken | None:
        return None

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        raise TokenError("invalid_grant", "refresh tokens are not issued")

    async def load_access_token(self, token: str) -> AccessToken | None:
        if token.startswith("s1_"):
            stored = self._session_tokens.get(token)
            if stored is None:
                return None
            now = int(time.time())
            idle_for = time.monotonic() - stored.last_used
            if (
                stored.access.expires_at is not None and stored.access.expires_at <= now
            ) or idle_for > self.settings.oauth_session_idle_ttl_seconds:
                self._session_tokens.pop(token, None)
                return None
            stored.last_used = time.monotonic()
            return stored.access

        payload = self._decrypt_token(token)
        if payload is None or payload.get("kind") != "access":
            return None
        try:
            expires_at = int(payload["exp"])
            if expires_at <= int(time.time()):
                return None
            return AccessToken(
                token=token,
                client_id=str(payload["client_id"]),
                scopes=[str(scope) for scope in payload["scopes"]],
                expires_at=expires_at,
                resource=payload.get("resource"),
                subject=str(payload["sub"]),
                claims={"iss": self.base_url, "credential_mode": "local"},
            )
        except (KeyError, TypeError, ValueError):
            return None

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        self._session_tokens.pop(token.token, None)

    def credentials_for_access_token(self, token: str) -> VikingCredentials | None:
        if token.startswith("s1_"):
            stored = self._session_tokens.get(token)
            return stored.credentials if stored is not None else None

        payload = self._decrypt_token(token)
        if payload is None or payload.get("kind") != "access":
            return None
        try:
            if int(payload["exp"]) <= int(time.time()):
                return None
            return VikingCredentials(
                email=str(payload["email"]),
                api_key=str(payload["api_key"]),
                role=str(payload["role"]),
            )
        except (KeyError, TypeError, ValueError):
            return None

    async def connect_page(self, request: Request) -> HTMLResponse | RedirectResponse:
        pending_id = request.path_params["pending_id"]
        pending = self._pending.get(pending_id)
        if pending is None or pending.expires_at <= time.time():
            self._pending.pop(pending_id, None)
            return self._render_page(
                pending_id,
                error="Ссылка устарела. Вернитесь в Codex и нажмите «Авторизоваться» ещё раз.",
                disabled=True,
            )

        if request.method == "GET":
            return self._render_page(pending_id)

        form = await request.form()
        mode = str(form.get("mode", ""))
        email = str(form.get("email", "")).strip()
        api_key = str(form.get("api_key", "")).strip()
        role = str(form.get("role", "trader")).strip().lower() or "trader"

        if mode not in {"session", "local"}:
            return self._render_page(pending_id, error="Сначала выберите один из двух вариантов.")
        if not email or not api_key:
            return self._render_page(
                pending_id,
                selected_mode=mode,
                email=email,
                role=role,
                error="Заполните email и API key.",
            )

        credentials = VikingCredentials(email=email, api_key=api_key, role=role)
        client = VikingClient(self.settings, credentials)
        try:
            await client.authenticate()
        except Exception:
            logger.warning("Viking OAuth credential validation failed", exc_info=True)
            return self._render_page(
                pending_id,
                selected_mode=mode,
                email=email,
                role=role,
                error="Viking не принял credentials. Проверьте email, API key и role.",
            )
        finally:
            await client.close()

        if pending.recovered_client is not None:
            try:
                await self._persist_client(pending.recovered_client)
            except OSError:
                logger.exception("Could not persist recovered OAuth client")
                return self._render_page(
                    pending_id,
                    selected_mode=mode,
                    email=email,
                    role=role,
                    error="Не удалось восстановить OAuth-сессию. Повторите попытку позже.",
                )

        self._pending.pop(pending_id, None)
        code_value = secrets.token_urlsafe(32)
        params = pending.params
        self._codes[code_value] = VikingAuthorizationCode(
            code=code_value,
            scopes=params.scopes or [OAUTH_SCOPE],
            expires_at=time.time() + 120,
            client_id=pending.client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
            subject=self._subject(credentials),
            credentials=credentials,
            mode=mode,
        )
        redirect_url = construct_redirect_uri(
            str(params.redirect_uri),
            code=code_value,
            state=params.state,
        )
        return RedirectResponse(redirect_url, status_code=302, headers={"Cache-Control": "no-store"})

    async def _cleanup_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(60)
                now = time.time()
                monotonic_now = time.monotonic()
                self._pending = {key: value for key, value in self._pending.items() if value.expires_at > now}
                self._codes = {key: value for key, value in self._codes.items() if value.expires_at > now}
                self._session_tokens = {
                    key: value
                    for key, value in self._session_tokens.items()
                    if (value.access.expires_at is None or value.access.expires_at > now)
                    and monotonic_now - value.last_used <= self.settings.oauth_session_idle_ttl_seconds
                }
        except asyncio.CancelledError:
            raise

    async def _persist_client(self, client_info: OAuthClientInformationFull) -> None:
        if not client_info.client_id:
            raise ValueError("OAuth client_id is required")
        async with self._clients_lock:
            clients = {**self._clients, client_info.client_id: client_info}
            await asyncio.to_thread(self._write_clients, clients)
            self._clients = clients

    def _load_clients(self) -> dict[str, OAuthClientInformationFull]:
        if not self._client_store_path.exists():
            return {}
        try:
            payload = json.loads(self._client_store_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("version") != 1:
                raise ValueError("unsupported OAuth client store format")
            raw_clients = payload.get("clients")
            if not isinstance(raw_clients, list):
                raise ValueError("OAuth client store must contain a client list")

            clients: dict[str, OAuthClientInformationFull] = {}
            for raw_client in raw_clients:
                client = OAuthClientInformationFull.model_validate(raw_client)
                if not client.client_id:
                    raise ValueError("stored OAuth client has no client_id")
                clients[client.client_id] = client
            return clients
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            logger.exception("Ignoring invalid OAuth client store at %s", self._client_store_path)
            return {}

    def _write_clients(self, clients: dict[str, OAuthClientInformationFull]) -> None:
        self._client_store_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "clients": [
                client.model_dump(mode="json", exclude_none=True)
                for client in clients.values()
            ],
        }
        temporary_path = self._client_store_path.with_name(
            f".{self._client_store_path.name}.{secrets.token_hex(8)}.tmp"
        )
        try:
            descriptor = os.open(
                temporary_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, self._client_store_path)
            os.chmod(self._client_store_path, 0o600)
        finally:
            temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _is_legacy_client_id(client_id: str) -> bool:
        try:
            parsed = UUID(client_id)
        except (ValueError, AttributeError):
            return False
        return parsed.version == 4 and str(parsed) == client_id

    def _encrypt_token(
        self,
        *,
        credentials: VikingCredentials,
        client_id: str,
        scopes: list[str],
        resource: str | None,
        subject: str,
        expires_at: int,
    ) -> str:
        payload = json.dumps(
            {
                "kind": "access",
                "email": credentials.email,
                "api_key": credentials.api_key,
                "role": credentials.role,
                "client_id": client_id,
                "scopes": scopes,
                "resource": resource,
                "sub": subject,
                "exp": expires_at,
            },
            separators=(",", ":"),
        ).encode()
        nonce = secrets.token_bytes(12)
        encrypted = self._cipher.encrypt(nonce, payload, TOKEN_AAD)
        encoded = base64.urlsafe_b64encode(nonce + encrypted).decode().rstrip("=")
        return f"v1_{encoded}"

    def _decrypt_token(self, token: str) -> dict[str, object] | None:
        if not token.startswith("v1_"):
            return None
        try:
            encoded = token[3:]
            padding = "=" * (-len(encoded) % 4)
            raw = base64.urlsafe_b64decode(encoded + padding)
            plaintext = self._cipher.decrypt(raw[:12], raw[12:], TOKEN_AAD)
            payload = json.loads(plaintext)
            return payload if isinstance(payload, dict) else None
        except (InvalidTag, ValueError, TypeError, json.JSONDecodeError):
            return None

    @staticmethod
    def _subject(credentials: VikingCredentials) -> str:
        value = f"{credentials.email.lower()}\0{credentials.role}".encode()
        return hashlib.sha256(value).hexdigest()

    def _render_page(
        self,
        pending_id: str,
        *,
        selected_mode: str = "",
        email: str = "",
        role: str = "trader",
        error: str = "",
        disabled: bool = False,
    ) -> HTMLResponse:
        selected_json = json.dumps(selected_mode)
        error_html = f'<div class="error">{html.escape(error)}</div>' if error else ""
        disabled_attr = " disabled" if disabled else ""
        form_hidden = " hidden" if not selected_mode else ""
        page = f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Подключение Viking</title>
  <style>
    :root {{ color-scheme: dark; font-family: Inter, system-ui, sans-serif; }}
    body {{ margin: 0; background: #0d0f12; color: #f7f8fa; }}
    main {{ max-width: 760px; margin: 0 auto; padding: 56px 24px; }}
    h1 {{ font-size: clamp(32px, 6vw, 52px); margin: 0 0 12px; }}
    .lead {{ color: #b9c0cc; font-size: 18px; margin-bottom: 34px; }}
    .modes {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
    .mode {{ min-height: 150px; text-align: left; border: 1px solid #3a414e; border-radius: 16px;
             padding: 22px; background: #171a20; color: inherit; cursor: pointer; }}
    .mode:hover, .mode.selected {{ border-color: #5d8cff; background: #1b2435; }}
    .mode strong {{ display: block; font-size: 22px; margin-bottom: 10px; }}
    .mode span {{ color: #b9c0cc; font-size: 15px; line-height: 1.45; }}
    form {{ margin-top: 24px; border: 1px solid #303642; border-radius: 16px; padding: 24px;
            background: #14171c; }}
    form.hidden {{ display: none; }}
    label {{ display: block; margin: 14px 0 7px; font-weight: 650; }}
    input {{ box-sizing: border-box; width: 100%; padding: 13px 14px; border-radius: 10px;
             border: 1px solid #414958; background: #0f1115; color: white; font-size: 16px; }}
    .submit {{ width: 100%; margin-top: 22px; border: 0; border-radius: 11px; padding: 14px;
               background: #3777f0; color: white; font-size: 17px; font-weight: 700; cursor: pointer; }}
    .error {{ margin: 18px 0; padding: 13px 15px; border: 1px solid #8d3c48; border-radius: 10px;
              background: #331820; color: #ffd8df; }}
    .security {{ margin-top: 22px; color: #8f98a8; font-size: 14px; }}
    @media (max-width: 620px) {{ .modes {{ grid-template-columns: 1fr; }} }}
  </style>
</head>
<body>
<main>
  <h1>Подключение Viking</h1>
  <p class="lead">Выберите один вариант. Больше ничего устанавливать не нужно.</p>
  {error_html}
  <div class="modes">
    <button class="mode" type="button" data-mode="session"{disabled_attr}>
      <strong>Только на эту сессию</strong>
      <span>Credentials находятся только в оперативной памяти и удаляются после бездействия.</span>
    </button>
    <button class="mode" type="button" data-mode="local"{disabled_attr}>
      <strong>Запомнить на этом компьютере</strong>
      <span>Codex сохранит зашифрованный токен локально. Railway не ведёт пользовательскую базу.</span>
    </button>
  </div>
  <form method="post" class="{form_hidden.strip()}">
    <input type="hidden" name="mode" id="mode" value="{html.escape(selected_mode)}">
    <label for="email">Email</label>
    <input id="email" name="email" type="email" autocomplete="email"
           value="{html.escape(email)}" required>
    <label for="api_key">API key</label>
    <input id="api_key" name="api_key" type="password" autocomplete="off" required>
    <label for="role">Role</label>
    <input id="role" name="role" value="{html.escape(role)}" required>
    <button class="submit" type="submit">Подключить</button>
  </form>
  <p class="security">API key не передаётся модели и не записывается в логи.</p>
</main>
<script>
  const selected = {selected_json};
  const form = document.querySelector("form");
  const modeInput = document.getElementById("mode");
  const buttons = [...document.querySelectorAll(".mode")];
  function choose(mode) {{
    modeInput.value = mode;
    form.classList.remove("hidden");
    buttons.forEach(button => button.classList.toggle("selected", button.dataset.mode === mode));
    document.getElementById("email").focus();
  }}
  buttons.forEach(button => button.addEventListener("click", () => choose(button.dataset.mode)));
  if (selected) choose(selected);
</script>
</body>
</html>"""
        return HTMLResponse(
            page,
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": (
                    "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                    "base-uri 'none'; frame-ancestors 'none'"
                ),
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
            },
        )
