"""One-shot integration; removed by the integration workflow after tests pass."""
from pathlib import Path
import subprocess

EXPECTED = {
    'app/main.py': 'c8ee3aaec7f03f38eff4b8c22d94aa0773f05b96',
    'app/oauth.py': '09a71b309e9cdbfaf151f7796a8bcda402ce89cc',
    'app/portfolio_tools.py': '19cee1ce72bd8a7e46784323826f106d512fe28d',
    'tests/test_mcp.py': '189157061ce05df79eda8dc97e0f45b2c4903e19',
    'AGENTS.md': '7777a6c335cd23f4ac34fd571c5bf156273fb4d4',
}
for path, expected in EXPECTED.items():
    assert subprocess.check_output(['git', 'hash-object', path], text=True).strip() == expected, path


def one(text, old, new):
    assert text.count(old) == 1, (old, text.count(old))
    return text.replace(old, new, 1)


p = Path('app/main.py')
s = p.read_text()
s = one(s, 'from mcp.server.fastmcp import FastMCP\n', '')
s = one(s, 'from app.config import get_settings\n', '''from app.auth_compat import (
    CompatibleFastMCP, OAuthCompatibilityMiddleware, metadata_routes, offered_scopes, register_auth_status,
)
from app.config import get_settings
''')
s = one(s, 'mcp = FastMCP(', 'mcp = CompatibleFastMCP(')
s = one(s, 'default_scopes=[OAUTH_SCOPE],', 'default_scopes=offered_scopes(settings),')
s = one(s, '_mcp_http_app = mcp.streamable_http_app()', 'register_auth_status(mcp, settings)\n\n_mcp_http_app = mcp.streamable_http_app()')
s = one(s, '        Route("/health", health, methods=["GET"]),', '        *metadata_routes(settings),\n        Route("/health", health, methods=["GET"]),')
s = one(s, '_mcp_cors_app = CORSMiddleware(\n    _starlette_app,', '''_auth_compat_app = OAuthCompatibilityMiddleware(_starlette_app, settings=settings, provider=oauth_provider)
_mcp_cors_app = CORSMiddleware(
    _auth_compat_app,''')
s = one(s, 'expose_headers=["Mcp-Session-Id"],', 'expose_headers=["Mcp-Session-Id", "WWW-Authenticate"],')
s = one(s, 'await _starlette_app(scope, receive, send)', 'await _auth_compat_app(scope, receive, send)')
p.write_text(s)

p = Path('app/portfolio_tools.py')
s = p.read_text()
a = s.index('WRITE_TOOL_NAMES = frozenset(')
b = s.index('\nWRITE = ToolAnnotations', a)
s = s[:a] + s[b:]
s = one(s, 'from app.config import Settings\n', '''from app.auth_compat import WRITE_TOOL_NAMES as WRITE_TOOL_NAMES
from app.auth_compat import write_auth_error
from app.config import Settings
''')
s = one(s, '''                    raise PermissionError(
                        "This OAuth grant is read-only. Reauthorize with viking.portfolio.write "
                        "and consent in the browser. Old grants are not upgraded automatically."
                    )''', '                    return write_auth_error(settings)')
p.write_text(s)

p = Path('app/oauth.py')
s = p.read_text()
s = one(s, 'import secrets\n', 'import secrets\nimport sqlite3\n')
s = one(s, 'from app.credentials import VikingCredentials\n', 'from app.credentials import VikingCredentials\nfrom app.grant_versions import GrantVersions\n')
s = one(s, '    mode: CredentialMode\n', '    mode: CredentialMode\n    grant_generation: int = 0\n')
s = one(s, '    absolute_expires_at: int\n', '    absolute_expires_at: int\n    grant_generation: int = 0\n')
s = one(s, '        self._clients = self._load_clients()\n', '        self._grants = GrantVersions(self._client_store_path.with_name("oauth-grants.sqlite3"))\n        self._clients = self._load_clients()\n')
s = one(s, '        requested_scopes = set(params.scopes or [OAUTH_SCOPE])\n', '''        if params.resource not in (None, f"{self.base_url}/mcp"):
            raise AuthorizeError("invalid_request", "resource must identify this MCP server")
        requested_scopes = set(params.scopes or [OAUTH_SCOPE])
''')
s = one(s, 'raise AuthorizeError("invalid_scope", "Requested scope is not registered for this client")', '''raise AuthorizeError(
                "invalid_scope",
                "Requested scope is not registered for this client. Reconnect with a new registration "
                "using the advertised scopes; existing read-only tokens remain usable.",
            )''')
s = one(s, '        scopes = stored.scopes or [OAUTH_SCOPE]\n', '''        scopes = await self._effective_scopes(
            stored.client_id, self._subject(stored.credentials),
            stored.scopes or [OAUTH_SCOPE], stored.grant_generation,
        )
''')
s = one(s, '                absolute_expires_at=now + self.settings.oauth_session_max_ttl_seconds,\n', '                absolute_expires_at=now + self.settings.oauth_session_max_ttl_seconds,\n                grant_generation=stored.grant_generation,\n')
s = one(s, '            expires_at=now + ttl,\n', '            expires_at=now + ttl,\n            grant_generation=stored.grant_generation,\n')
s = one(s, '''        return self._issue_session_tokens(
            credentials=stored.credentials,
            client_id=stored.refresh.client_id,
            scopes=requested_scopes,''', '''        effective_scopes = await self._effective_scopes(
            stored.refresh.client_id, stored.refresh.subject or self._subject(stored.credentials),
            requested_scopes, stored.grant_generation,
        )
        return self._issue_session_tokens(
            credentials=stored.credentials,
            client_id=stored.refresh.client_id,
            scopes=effective_scopes,''')
s = one(s, '            absolute_expires_at=stored.absolute_expires_at,\n', '            absolute_expires_at=stored.absolute_expires_at,\n            grant_generation=stored.grant_generation,\n')
s = one(s, '''            stored.last_used = time.monotonic()
            return stored.access''', '''            if stored.access.resource not in (None, f"{self.base_url}/mcp"):
                return None
            stored.last_used = time.monotonic()
            effective = await self._effective_scopes(
                stored.access.client_id, stored.access.subject or self._subject(stored.credentials),
                stored.access.scopes, (stored.access.claims or {}).get("grant_generation", 0),
            )
            return stored.access.model_copy(update={"scopes": effective})''')
s = one(s, '''            return AccessToken(
                token=token,''', '''            if payload.get("resource") not in (None, f"{self.base_url}/mcp"):
                return None
            effective = await self._effective_scopes(
                str(payload["client_id"]), str(payload["sub"]),
                [str(scope) for scope in payload["scopes"]], payload.get("grant_generation", 0),
            )
            return AccessToken(
                token=token,''')
s = one(s, '                scopes=[str(scope) for scope in payload["scopes"]],', '                scopes=effective,')
s = one(s, '                claims={"iss": self.base_url, "credential_mode": "local"},', '                claims={"iss": self.base_url, "credential_mode": "local", "grant_generation": payload.get("grant_generation", 0)},')
s = one(s, '    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:\n', '''    async def _effective_scopes(
        self, client_id: str, subject: str, scopes: list[str], generation: int,
    ) -> list[str]:
        if PORTFOLIO_WRITE_SCOPE not in scopes:
            return list(scopes)
        try:
            current = await asyncio.to_thread(self._grants.current, client_id, subject)
        except (OSError, sqlite3.Error, ValueError):
            logger.error("Cannot read OAuth grant policy; write scope denied, read scope retained")
            return [scope for scope in scopes if scope != PORTFOLIO_WRITE_SCOPE]
        if generation != current:
            return [scope for scope in scopes if scope != PORTFOLIO_WRITE_SCOPE]
        return list(scopes)

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
''')
s = one(s, '        if PORTFOLIO_WRITE_SCOPE in (pending.params.scopes or []) and (\n', '''        selected_access = str(form.get("access_mode", ""))
        granted_scopes = list(pending.params.scopes or [OAUTH_SCOPE])
        if selected_access not in {"", "read", "write"} or (
            selected_access == "write" and PORTFOLIO_WRITE_SCOPE not in granted_scopes
        ):
            return self._render_page(
                pending_id, selected_mode=mode, email=email, role=role,
                error="Выбранные права не входят в запрос клиента. Начните новую авторизацию.",
            )
        if selected_access == "read":
            granted_scopes = [OAUTH_SCOPE]
        if PORTFOLIO_WRITE_SCOPE in granted_scopes and (
''')
s = one(s, '''        self._pending.pop(pending_id, None)
        code_value = secrets.token_urlsafe(32)''', '''        try:
            # Explicit read choice revokes old write grants only for this client/user/role.
            # Legacy form POSTs without access_mode retain their existing semantics.
            operation = self._grants.downgrade if selected_access == "read" else self._grants.current
            generation = await asyncio.to_thread(operation, pending.client_id, self._subject(credentials))
        except (OSError, sqlite3.Error, ValueError):
            logger.error("Could not persist OAuth permission choice")
            return self._render_page(
                pending_id, selected_mode=mode, email=email, role=role,
                error="Не удалось сохранить выбор прав. Повторите авторизацию позже.",
            )
        self._pending.pop(pending_id, None)
        code_value = secrets.token_urlsafe(32)''')
s = one(s, '            scopes=params.scopes or [OAUTH_SCOPE],\n', '            scopes=granted_scopes,\n            grant_generation=generation,\n')
s = one(s, '        absolute_expires_at: int,\n', '        absolute_expires_at: int,\n        grant_generation: int = 0,\n')
s = one(s, '            claims={"iss": self.base_url, "credential_mode": "session"},', '            claims={"iss": self.base_url, "credential_mode": "session", "grant_generation": grant_generation},')
s = one(s, '            absolute_expires_at=absolute_expires_at,\n', '            absolute_expires_at=absolute_expires_at,\n            grant_generation=grant_generation,\n')
s = one(s, '        expires_at: int,\n', '        expires_at: int,\n        grant_generation: int = 0,\n')
s = one(s, '                "exp": expires_at,\n', '                "exp": expires_at,\n                "grant_generation": grant_generation,\n')
s = one(s, '''        write_consent_html = ""
        if write_requested:''', '''        write_consent_html = (
            '<input type="hidden" name="access_mode" value="read">'
            '<p>Клиент запросил только чтение. Для управления портфелями начните '
            'повторное подключение с расширенными правами.</p>'
        )
        if write_requested:''')
s = one(s, '''            write_consent_html = (
                '<div class="error">''', '''            write_consent_html = (
                '<fieldset><legend>Доступ этого подключения</legend>'
                '<label><input type="radio" name="access_mode" value="read" checked '
                'style="width:auto"> Только чтение (отозвать прежнее право записи этого подключения)</label>'
                '<label><input type="radio" name="access_mode" value="write" '
                'style="width:auto"> Чтение и управление портфелями</label></fieldset>'
                '<div class="error">''')
s = one(s, '''                'style="width:auto" required> Разрешаю изменение пользовательских полей и остановки ' '' '.replace(' '' ', ''), '''                'style="width:auto"> Разрешаю изменение пользовательских полей и остановки ' '' '.replace(' '' ', '')) if False else s
s = one(s, 'style="width:auto" required> Разрешаю', 'style="width:auto"> Разрешаю')
s = one(s, '        selected_json = json.dumps(selected_mode)\n', '''        if pending:
            target_host = html.escape(urlparse(str(pending.params.redirect_uri)).netloc)
            write_consent_html = (
                f'<p>Получатель авторизации: <strong>{target_host}</strong>. '
                'Проверьте адрес приложения перед вводом ключа.</p>' + write_consent_html
            )
        selected_json = json.dumps(selected_mode)
''')
s = one(s, 'Codex сохранит зашифрованный токен локально. Railway не ведёт пользовательскую базу.', 'Ваше ИИ-приложение сохранит зашифрованный токен. У облачного клиента он хранится на его стороне.')
s = s.replace('Вернитесь в Codex и нажмите «Авторизоваться» ещё раз.', 'Вернитесь в ваше ИИ-приложение и начните авторизацию ещё раз.')
p.write_text(s)

p = Path('tests/test_mcp.py')
s = p.read_text()
s = one(s, '    assert set(tools) == {\n', '    assert set(tools) == {\n        "get_authorization_status",\n')
p.write_text(s)

p = Path('README.md')
s = p.read_text().replace('Он предоставляет 55 инструментов:', 'Он предоставляет 56 инструментов:')
s = one(s, '- `update_portfolio_user_fields`', '- `get_authorization_status` — текущие права подключения без запроса к Viking;\n- `update_portfolio_user_fields`')
s += '''

## OAuth-совместимость ИИ-клиентов

Сервер публикует `viking.read` и `viking.portfolio.write` в protected-resource
metadata, когда запись включена; при выключенной записи публикуется только чтение.
Начальный HTTP 401 содержит `resource_metadata`, но не принудительный read-only
scope. Claude и другие клиенты discovery могут запросить объявленные права.
При DCR без scope сервер регистрирует объявленный набор; явно переданный scope
не расширяется. Регистрация — допустимый набор запроса, а не согласие пользователя.
`required_scopes` /mcp остаётся только `viking.read`.

На странице авторизации по умолчанию выбрано «Только чтение». Если клиент запросил
оба scope, можно выбрать «Чтение и управление портфелями» и отдельно отметить
согласие. Только после него выдаётся запись. Выбор чтения сужает token.scope;
клиент должен использовать реально выданный scope, а не исходно запрошенный.
Явно read-only запрос не может быть расширен фальшивой галочкой.

Недостаток scope при подтверждённом вызове записи даёт HTTP 403 с Bearer
`insufficient_scope`, обоими необходимыми scope и `resource_metadata`. Тело
сохраняет MCP `isError` и `_meta["mcp/www_authenticate"]`; прямые вызовы инструментов
тоже возвращают эту metadata. Descriptor каждого инструмента содержит
`securitySchemes` и зеркало в `_meta`. Никакого автоматического исполнения после
OAuth сервер не делает. Клиент должен ограничивать повторы и сохранять согласованные
цели/параметры. Поскольку metadata инструментов описывает исполнение, некоторые
клиенты могут запрашивать запись даже перед preview; сервер разрешает preview с read.

`get_authorization_status` безопасно возвращает scope текущего bearer token,
`can_write`, серверный переключатель и причину отказа. Не обращается к Viking и
не подтверждает права роли на конкретный портфель. Нет токенов/email/API key в ответе.

Старые токены и форматы сохраняются. Явный выбор «Только чтение» в новой форме
отзывает write у прежних access/refresh токенов **того же client_id и пользователя
с той же ролью**; чтение сохраняется. Это не отзывает другие приложения/аккаунты.
Новая регистрация client_id — другое подключение, она не отзывает потерянную старую.
Смена роли — отдельная авторизованная идентичность; уменьшайте права под прежней ролью.
Политика поколений хранится в `oauth-grants.sqlite3` рядом с OAuth client store,
права файла 0600; только хэши идентичностей и счётчики, без credentials и токенов.
Не удалять этот файл при deploy/rollback: удаление теряет сохранённые отзывы.
Для нескольких реплик нужен общий поддерживаемый store, а не независимые копии.

Сохранён старый программный flow Lovable/K1FORGE: `/register`, `/authorize`, POST
`/oauth/connect/{id}` с прежними полями и `allow_portfolio_writes=yes`, затем `/token`.
Новый `access_mode` необязателен для старых клиентов. Refresh не повышает права.
При устаревшей регистрации только read нужен один reconnect с новой регистрацией,
использующей metadata; повторение refresh само по себе не поможет.

HTTP 403 вместо прежнего HTTP 200 с ошибкой записи — намеренное изменение только
пути **неразрешённой записи**. Старому frontend может понадобиться обработчик
`insufficient_scope`, который предлагает reauthorize, но не отключает чтение и
не повторяет сам торговую команду. CORS раскрывает `WWW-Authenticate` разрешённым
origin. Имена, аргументы и успешные результаты прежних инструментов не изменены.

Приёмка и ограничения: [матрица OAuth](docs/oauth-compatibility.md).
'''
p.write_text(s)

p = Path('AGENTS.md')
s = p.read_text().replace('реализованы 55 MCP-инструментов: 50 инструментов чтения', 'реализованы 56 MCP-инструментов: 51 инструмент чтения')
s += '''

## OAuth interoperability (2026-09-23)

Актуальный контракт OAuth дополняет секцию 5: `app/auth_compat.py` публикует полный
предлагаемый scope при включённой записи, не делает write обязательным для чтения.
Старые явно заданные scope не расширять. При запросе обоих scope новая форма
`access_mode=read/write` сужает grant до read по умолчанию; write требует прежнего
allow_portfolio_writes=yes. Legacy POST без access_mode сохраняет старый контракт
Lovable. Не добавлять обязательный JS/cookie/новое поле к этому программному flow.

Перед выполнением подтверждённой записи без scope HTTP middleware возвращает
403 + WWW-Authenticate insufficient_scope и MCP error metadata; в Viking ничего
не отправляется. Прямой tool-result тоже несёт mcp/www_authenticate. Старый frontend
может потребовать адаптацию именно error path 403, не успешных данных/подписок.
Не превращать API rejection/тайм-аут отправленной команды в auth challenge.
Не повторять автоматически торговые команды после OAuth.

56 инструментов: новый get_authorization_status — read-only, без вызова Viking,
без секретов; can_write описывает MCP, не права конкретной роли платформы.
Неизменные существующие схемы/аннотации дополняются securitySchemes metadata.

Явное снижение прав в новой форме повышает grant_generation в отдельном SQLite
store рядом с oauth-clients.json: только хэш (client_id, subject) и счётчик, не
credentials. Старые токены принимаются для чтения, но write и refresh не обходят
новый generation. Новые grants связываются с generation в момент авторизации,
не в момент обмена кода. Другие client_id/пользователи/роли не затрагиваются.
Не удалять store и не вращать ключ токенов ради миграции. Общая файловая политика
рассчитана на текущий single-replica volume; масштабирование требует общего store.
Ошибки чтения политики fail-closed для write, но не для read. Session tokens всё
ещё в RAM: рестарт потребует их переавторизации, это не исправлено этой задачей.

Тестировать полный DCR + S256 + form POST + code exchange + MCP 401/403 + refresh,
новый выбор read/write, старый Lovable flow, legacy v1_/s1_ и downgrade isolation.
Не выдавать симуляцию клиента за live Claude/ChatGPT/Codex. Матрица реальной приёмки
и client follow-ups хранятся в docs/oauth-compatibility.md. Viking wire-контракт
не меняется: роль по api.md остаётся отдельным пределом прав.
'''
p.write_text(s)
print('OAuth compatibility applied; no live Viking calls or runtime secrets used.')
