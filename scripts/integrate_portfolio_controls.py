"""Temporary, deterministic feature-branch integration; removed before merge."""
from pathlib import Path


def replace(path, old, new):
    p = Path(path)
    text = p.read_text()
    if old not in text and new in text:
        return
    assert text.count(old) == 1, (path, old[:90], text.count(old))
    p.write_text(text.replace(old, new))


replace("app/config.py", '    viking_ws_url: str = "wss://bot.fkviking.com/ws"',
        '    viking_portfolio_writes_enabled: bool = False\n\n    viking_ws_url: str = "wss://bot.fkviking.com/ws"')
replace("app/viking_client.py", '        self._send_lock = asyncio.Lock()',
        '        self._send_lock = asyncio.Lock()\n        self._portfolio_control_lock = asyncio.Lock()\n        self._portfolio_control_next_send_at = 0.0')
replace("app/viking_client.py", '    async def authenticate(self) -> None:', '''    async def execute_portfolio_control(
        self, *, robot_id: str, portfolio: str, action: str,
        fields: dict[str, Any] | None = None, side: str = "both",
    ) -> dict[str, Any]:
        """Allowlisted portfolio writes use a single send, never the retrying read request path."""
        from app.portfolio_control import send_control_once

        return await send_control_once(
            self, robot_id=robot_id, portfolio=portfolio, action=action, fields=fields, side=side
        )

    async def authenticate(self) -> None:''')

replace("app/oauth.py", 'OAUTH_SCOPE = "viking.read"',
        'OAUTH_SCOPE = "viking.read"\nPORTFOLIO_WRITE_SCOPE = "viking.portfolio.write"')
replace("app/oauth.py", '        recovered_client = None\n        if isinstance(client, RecoverableOAuthClient):', '''        requested_scopes = set(params.scopes or [OAUTH_SCOPE])
        registered_scopes = set((client.scope or OAUTH_SCOPE).split())
        if (
            OAUTH_SCOPE not in requested_scopes
            or not requested_scopes.issubset({OAUTH_SCOPE, PORTFOLIO_WRITE_SCOPE})
            or not requested_scopes.issubset(registered_scopes)
        ):
            raise AuthorizeError("invalid_scope", "Requested scope is not registered for this client")
        if PORTFOLIO_WRITE_SCOPE in requested_scopes and not self.settings.viking_portfolio_writes_enabled:
            raise AuthorizeError("invalid_scope", "Portfolio writes are disabled by the server administrator")
        recovered_client = None
        if isinstance(client, RecoverableOAuthClient):''')
replace("app/oauth.py", '        credentials = VikingCredentials(email=email, api_key=api_key, role=role)\n        client = VikingClient(self.settings, credentials)', '''        if PORTFOLIO_WRITE_SCOPE in (pending.params.scopes or []):
            if not self.settings.viking_portfolio_writes_enabled or form.get("allow_portfolio_writes") != "yes":
                return self._render_page(
                    pending_id, selected_mode=mode, email=email, role=role,
                    error="Для изменения полей и остановок нужно отдельное явное разрешение на операции записи.",
                )

        credentials = VikingCredentials(email=email, api_key=api_key, role=role)
        client = VikingClient(self.settings, credentials)''')
replace("app/oauth.py", '        selected_json = json.dumps(selected_mode)', '''        pending = self._pending.get(pending_id)
        write_requested = pending is not None and PORTFOLIO_WRITE_SCOPE in (pending.params.scopes or [])
        write_consent_html = ""
        if write_requested:
            write_consent_html = (
                '<div class="error">Клиент запрашивает изменение uf0–uf19 и остановку торговли. '
                'Stop formulas также отключает формулы. Эти операции могут влиять на торговлю.</div>'
                '<label><input type="checkbox" name="allow_portfolio_writes" value="yes" '
                'style="width:auto" required> Разрешаю изменение пользовательских полей и остановки '
                'портфелей через этот MCP-клиент (viking.portfolio.write).</label>'
            )
        selected_json = json.dumps(selected_mode)''')
replace("app/oauth.py", '    <button class="submit" type="submit">Подключить</button>',
        '    {write_consent_html}\n    <button class="submit" type="submit">Подключить</button>')

replace("app/main.py", 'from app.oauth import OAUTH_SCOPE, VikingOAuthProvider',
        'from app.oauth import OAUTH_SCOPE, PORTFOLIO_WRITE_SCOPE, VikingOAuthProvider\nfrom app.portfolio_tools import register_portfolio_control_tools')
replace("app/main.py", '            valid_scopes=[OAUTH_SCOPE],',
        '            valid_scopes=[OAUTH_SCOPE, PORTFOLIO_WRITE_SCOPE],')
replace("app/main.py", '"Если пользователь не назвал поля, используй buy, sell и pos. Сервер только читает данные. "',
        '"Если пользователь не назвал поля для чтения, используй buy, sell и pos. "\n'
        '        "Запись ограничена uf0..uf19 и остановками. Сначала покажи предпросмотр с точными "\n'
        '        "robot_id/portfolio и последствиями; исполняй только явно подтверждённую пользователем "\n'
        '        "операцию с dry_run=false, confirm=true. Не расширяй список целей и не заменяй Stop "\n'
        '        "на Hard stop или Stop formulas. accepted не доказывает остановку/снятие заявок. "\n'
        '        "При outcome_unknown не повторяй запись автоматически. "')
replace("app/main.py", '\n\n@mcp.tool(\n    title="Доступные портфели",',
        '\n\nregister_portfolio_control_tools(\n    mcp, settings=settings, service_factory=lambda: _service_for_request()\n)\n\n\n@mcp.tool(\n    title="Доступные портфели",')
replace("tests/test_mcp.py", '    assert set(tools) == {\n        "list_available_portfolios",',
        '    assert set(tools) == {\n        "update_portfolio_user_fields",\n        "stop_portfolio_trading",\n        "stop_portfolios",\n        "hard_stop_portfolios",\n        "stop_portfolio_formulas",\n        "list_available_portfolios",')

replace("README.md", 'Публичный read-only MCP-сервер поверх WebSocket API `bot.fkviking.com`.',
        'Публичный MCP-сервер поверх WebSocket API `bot.fkviking.com`: чтение данных и\nотдельно разрешаемые операции изменения пользовательских полей и остановки портфелей.')
replace("README.md", 'Он предоставляет 50 инструментов:', 'Он предоставляет 55 инструментов:')
replace("README.md", '- `list_available_portfolios` — доступные пользователю портфели;',
        '- `update_portfolio_user_fields` — частичное изменение `uf0`–`uf19`;\n'
        '- `stop_portfolio_trading` — выключение `re_sell` и/или `re_buy` одного портфеля;\n'
        '- `stop_portfolios` — Stop portfolios по явному списку;\n'
        '- `hard_stop_portfolios` — Hard stop по явному списку;\n'
        '- `stop_portfolio_formulas` — Stop formulas по явному списку;\n'
        '- `list_available_portfolios` — доступные пользователю портфели;')
replace("AGENTS.md", '`viking-marketdata-mcp` — публичный многопользовательский read-only MCP-сервер\nповерх WebSocket API `bot.fkviking.com`.',
        '`viking-marketdata-mcp` — публичный многопользовательский MCP-сервер чтения данных\nс отдельным разрешением на ограниченные операции управления портфелями поверх\nWebSocket API `bot.fkviking.com`.')
replace("AGENTS.md", 'В актуальной версии реализованы 50 MCP-инструментов. Сервер\nтолько читает данные: он не создаёт и не изменяет портфели, не меняет поля, не\nотправляет торговые сигналы и заявки.',
        'В актуальной версии реализованы 55 MCP-инструментов: 50 инструментов чтения и\n5 отдельно разрешаемых инструментов изменения uf0–uf19 и остановки портфелей.\nСервер не создаёт портфели, не включает торговлю, не выставляет заявки и не\nотправляет торговые сигналы. Hard stop/Stop formulas могут снимать существующие заявки.')
replace("AGENTS.md", 'Все инструменты этого MCP являются read-only. Если пользовательская задача может быть',
        'Прежние 50 инструментов этого MCP остаются read-only. Если пользовательская задача может быть')

readme = '''

## Управление портфелями: uf0–uf19 и остановки

По умолчанию сервер **не исполняет операции записи**. Администратор включает их
через `VIKING_PORTFOLIO_WRITES_ENABLED=true`. Это дополнительный серверный
предохранитель; права роли Viking продолжают проверяться самой платформой.

OAuth-клиент должен зарегистрировать и запросить **оба** scope:
`viking.read viking.portfolio.write`. На странице авторизации появится отдельное
неотмеченное согласие на изменение полей и остановки. Старые токены остаются
read-only; refresh не повышает права. Регистрации только с `viking.read` необходимо
перерегистрировать с расширенным scope средствами клиента, затем пройти OAuth
заново. Одного перезапуска сервера или обновления старого токена недостаточно.
Клиент, который не умеет запрашивать дополнительный scope, сможет только читать и
делать предпросмотр. Два режима хранения credentials не меняются.

Все пять инструментов помечены как изменяющие состояние. Подтверждения клиента для
них ожидаемы; read-only инструменты сохраняют прежние аннотации. Параметры
`confirm` и `dry_run` — защита от случайного вызова, а не доказательство того, что
человек просмотрел результат: агент обязан получить явное согласие пользователя.

### Пользовательские поля

`update_portfolio_user_fields` принимает один портфель. Число сокращает объект
`{"v": число}`; `c` — подпись. Можно менять значение, подпись или оба ключа.
Неуказанные поля и неуказанные ключи сохраняются. Проверяются актуальный шаблон,
редактируемость, диапазон `min/max` и `max_len` подписи в UTF-8 байтах.
Булевы значения, строки вместо числа, null, NaN/Infinity и поля вне uf0–uf19 запрещены.

Пример **предпросмотра**, имена ниже условные:

```json
{
  "robot_id": "test-robot",
  "portfolio": "example",
  "fields": {"uf0": 12.5, "uf1": {"v": 0}, "uf19": {"c": "Limit"}},
  "dry_run": true
}
```

После проверки плана и явного согласия пользователя тот же инструмент вызывается
с теми же целями и значениями, `dry_run=false`, `confirm=true`. Результат записи
нужно отдельно проверить чтением нужных полей через `get_current_portfolio_data`.
Пользовательские поля могут использоваться формулами и влиять на торговлю.

### Прямая и массовая остановка

| Инструмент | Viking API | Отличие |
| --- | --- | --- |
| `stop_portfolio_trading` | `portfolio.update` | `side=both/sell/buy`; меняет только соответствующие re-флаги на false |
| `stop_portfolios` | `portfolio.update` для каждой цели | Оба re-флага false; расписание и формулы сохраняются; заявки второй ноги могут продолжать работу |
| `hard_stop_portfolios` | `portfolio.hard_stop` для каждой цели | Остановка и снятие заявок обеих ног; расписание выключается, формулы сохраняются |
| `stop_portfolio_formulas` | `portfolio.formulas_stop` для каждой цели | Также отключает формулы, меняет формульные режимы на константы/Standard; восстановление вручную |

Массовые инструменты принимают `targets` — от 1 до 200 точных пар:

```json
{
  "targets": [
    {"robot_id": "test-robot", "portfolio": "example-a"},
    {"robot_id": "test-robot", "portfolio": "example-b"}
  ],
  "dry_run": true
}
```

Wildcards, пустые списки и дубликаты запрещены. Для остановки всех портфелей сначала
прочитайте список, покажите точный состав пользователю и передайте подтверждённые
цели явно. Один и тот же список не означает согласия на все три вида остановки.
Операции **не отправляют команду закрытия позиций**. Обычный Stop не защищает от
повторного включения формулами/расписанием; Hard stop оставляет формулы активными.

Пакет выполняется последовательно, максимум 10 отправок в секунду на соединение
credentials, без атомарности и rollback. Полный список валидируется до первой записи.
Ответ содержит `items` и `counts`: `preview`, `accepted`, `rejected`,
`outcome_unknown`, `not_sent`. При отклонении одной цели обработка следующих
продолжается. После сбоя транспорта оставшаяся часть не отправляется.
**Автоматического повтора нет**, в том числе после timeout/reconnect: команда могла
уже выполниться. `accepted` означает ответ Viking `r=p`, не подтверждение исполнения
биржей; `verified=false` возвращается честно. Состояние торговли проверяйте через
`get_robot_portfolio_trading_status`, активные заявки — через инструменты заявок.
Предпросмотр не фиксирует состояние: шаблон, права и состояние могут измениться.

Контракты: [Viking WebSocket API](https://github.com/fkviking/bot-doc/blob/master/assets/ru/api.md).
Семантика остановок: [интерфейс платформы](https://fkviking.github.io/bot-doc/docs/interface.html).
'''
p = Path("README.md")
if "## Управление портфелями: uf0–uf19 и остановки" not in p.read_text():
    p.write_text(p.read_text() + readme)

agents = '''

## Portfolio controls: явное исключение из read-only (2026-09-18)

Эта секция заменяет прежний общий запрет на изменение полей **только** для пяти
перечисленных инструментов. Прочие ограничения и 50 read-only инструментов неизменны.

- `update_portfolio_user_fields`: `portfolio.update`, `data.r_id`,
  `data.portfolio.name`, только `uf0`–`uf19`, частичные `{v,c}`. До записи:
  `get_template_id` → `get_template_by_id`, проверка ID, editor/disabled, min/max,
  UTF-8 max_len. Не сливать patch со snapshot, не передавать securities.
- `stop_portfolio_trading`: `portfolio.update` только re_sell/re_buy=false,
  side=both/sell/buy. Нельзя передавать true или другие поля.
- `stop_portfolios`: те же два false по каждой явно указанной цели.
- `hard_stop_portfolios`: `portfolio.hard_stop`, data={r_id,p_id}.
  В API-примере встречается опечатка `portfolio.hasrd_stop`: не использовать её.
- `stop_portfolio_formulas`: `portfolio.formulas_stop`, data={r_id,p_id}.

Архитектура записи: `app/portfolio_tools.py` → `PortfolioControlService` в
`app/portfolio_control.py` → `VikingClient.execute_portfolio_control` →
`send_control_once` → `_request_connected`. Пул соединений тот же; credential data
не появляются в аргументах инструментов. Для записи нельзя использовать
`VikingClient.request`: он повторяет запросы чтения при ошибке транспорта.

Защита: серверный флаг `VIKING_PORTFOLIO_WRITES_ENABLED` по умолчанию false;
отдельный scope `viking.portfolio.write` вместе с `viking.read`; явное согласие
на OAuth-странице; dry_run=true по умолчанию; исполнение только при строгих JSON
boolean dry_run=false и confirm=true. Старые grants и refresh не повышают права.
MCP annotations: readOnlyHint=false, destructiveHint=true, idempotentHint=false,
openWorldHint=true. Не снимать подтверждения для mutating-инструментов ради удобства.

Сначала показать точный план и последствия и получить согласие пользователя.
Не расширять цели и не усиливать Stop до Hard stop/Stop formulas автоматически.
Вызов confirm=true сам по себе не удостоверяет согласие человека. Stop formulas
может менять режимы формул; восстановление не автоматическое. Не применять disabled
как способ остановки торговли. Не отправлять команды закрытия позиции.

Пакеты: явный список 1..200 уникальных пар robot_id/portfolio, весь список
валидируется до отправки. Последовательные sends, локальный предел 10/с на
credentials, без атомарности/rollback. API rejection сохраняется вместе с code
и response; дальнейшие цели обрабатываются. При транспортном сбое текущая цель
outcome_unknown (или not_sent при провале подключения до send), остальные not_sent.
Автоповтора нет; cancellation после начала send тоже не даёт основания повторять.
Ответ r=p/accepted — только API acknowledgement; проверять type, eid, ts, r и
r_id/p_id. verified=false до отдельной проверки. Состояние торговли определять
через robot.subscribe / get_robot_portfolio_trading_status, а не disabled.

Проверки: `tests/test_portfolio_controls.py`, `tests/test_portfolio_control_oauth.py`;
полный `uv run pytest -q`, `uv run ruff check app tests`. README и число 55 инструментов
обязательны для этой поставки. Live-проверки остановок разрешены только на явно
выбранных пользователем тестовых портфелях. Этот PR сам по себе не разрешает merge,
production deploy или реальные операции с портфелями.
'''
p = Path("AGENTS.md")
if "## Portfolio controls: явное исключение" not in p.read_text():
    p.write_text(p.read_text() + agents)
p = Path(".env.example")
if "VIKING_PORTFOLIO_WRITES_ENABLED" not in p.read_text():
    p.write_text(p.read_text() + "\n# Explicitly enable scoped portfolio writes; read tools and previews remain available.\nVIKING_PORTFOLIO_WRITES_ENABLED=false\n")
print("Portfolio controls integrated; changes remain on the feature branch.")
