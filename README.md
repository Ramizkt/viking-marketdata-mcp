# Viking Market Data MCP

Публичный read-only MCP-сервер поверх WebSocket API `bot.fkviking.com`.

Он предоставляет девять инструментов:

- `list_available_portfolios` — доступные пользователю портфели;
- `subscribe_available_portfolios` — подписка на изменения списка портфелей;
- `get_available_portfolio_updates` — чтение накопленных событий подписки;
- `unsubscribe_available_portfolios` — явное завершение подписки;
- `get_current_portfolio_data` — полный текущий снапшот выбранного портфеля;
- `subscribe_portfolio` — подписка на все изменения выбранного портфеля;
- `get_portfolio_updates` — чтение обновлений полей портфеля и его инструментов;
- `unsubscribe_portfolio` — явное завершение подписки на портфель;
- `get_portfolio_data` — история выбранного портфеля за период.

Небольшой результат возвращается непосредственно в MCP. Большой результат
сохраняется в CSV и возвращается временной подписанной ссылкой.

## Подключение

Публичный адрес сервера:

```text
https://viking-marketdata-mcp-production.up.railway.app/mcp
```

Выберите свой MCP-клиент:

- [Codex](#codex);
- [Claude Code](#claude-code).

Credentials вводятся только на странице авторизации. Они не являются
аргументами MCP-инструментов, не передаются модели и не должны отправляться в чат.

### Codex

1. Откройте **Настройки → Плагины → MCP**.
2. Нажмите **Добавить сервер**.
3. Заполните поля:

   - **Имя:** `Viking Market Data`;
   - **Тип:** `Потоковая передача HTTP`;
   - **URL:**

```text
https://viking-marketdata-mcp-production.up.railway.app/mcp
```

4. Остальные поля оставьте пустыми и нажмите **Сохранить**.
5. Откройте настройки добавленного сервера и нажмите **Авторизоваться**.
6. На странице Viking выберите режим хранения credentials:

   - **Только на эту сессию** — Viking credentials находятся только в RAM
     Railway. Они удаляются после 15 минут без запросов, завершения сессии или
     перезапуска сервера. Следующий запрос после завершения сессии автоматически
     запустит повторную авторизацию.
   - **Запомнить на этом компьютере** — Codex хранит локальный OAuth-токен.
     Viking credentials зашифрованы внутри токена; Railway не сохраняет их в
     базе данных или файлах.

7. Введите `Email`, `API key` и `Role`, затем нажмите **Подключить**.
8. Создайте новый чат и отправьте:

```text
Покажи доступные мне портфели в Viking.
```

### Claude Code

1. Откройте PowerShell, Terminal или другую командную строку.
2. Добавьте сервер для текущего пользователя:

```bash
claude mcp add --transport http viking-market-data --scope user https://viking-marketdata-mcp-production.up.railway.app/mcp
```

3. Запустите авторизацию:

```bash
claude mcp login viking-market-data
```

4. В открывшемся браузере выберите режим хранения credentials, введите
   `Email`, `API key` и `Role`, затем нажмите **Подключить**.
5. Запустите Claude Code:

```bash
claude
```

6. Отправьте запрос:

```text
Покажи доступные мне портфели в Viking.
```

Проверить подключение можно командами:

```bash
claude mcp get viking-market-data
claude mcp list
```

Также состояние сервера можно посмотреть внутри Claude Code командой `/mcp`.

Если браузер завершил авторизацию, но не смог вернуться в Claude Code,
скопируйте полный URL из адресной строки браузера и вставьте его в запрос URL,
появившийся в терминале.

## MCP tools

### `list_available_portfolios`

Параметр `history_only=true` оставляет только портфели, по которым доступна
история. Для каждого результата возвращаются `robot_id`, `portfolio`, `owner`
и `history_available`.

### Подписка на список доступных портфелей

1. `subscribe_available_portfolios` создаёт подписку
   `available_portfolio_list.subscribe` и возвращает первоначальный снапшот.
2. `get_available_portfolio_updates` возвращает последующие события
   `portfolios_add` и `portfolios_del`. Для короткого ожидания можно передать
   `wait_seconds` от 0 до 30.
3. `unsubscribe_available_portfolios` закрывает подписку по `subscription_id`.

Для каждого портфеля без потерь возвращаются все поля, определённые этим методом
Viking API: `robot_id`, `portfolio`, `owner`. Ответы также содержат служебные поля
`type`, `eid`, `ts`, исходный код результата `r`, его читаемый алиас `result` и
объект `data`.

Важно: сама подписка на список не возвращает торговые поля портфеля (`buy`,
`sell`, `pos`, `uf0`…`uf19`). Согласно Viking API, этот метод содержит только
идентификатор робота, имя портфеля и владельца. Текущее полное состояние
запрашивается отдельно через `get_current_portfolio_data`, а история полей —
через `get_portfolio_data`.

Ошибки Viking с `r="e"` возвращаются MCP-клиенту как ошибки инструмента с
исходными `code`, `msg` и полным API-ответом. Повреждённые ответы, неожиданные
коды результата, потеря соединения и переполнение буфера подписки обрабатываются
отдельно и не выдаются как успешные события. После потери соединения нужно
создать новую подписку.

### Текущие данные и подписка на портфель

Официальный Viking API не содержит операции с буквальным типом
`get_portfolio_data`. Текущее полное состояние возвращает первоначальный
снапшот `portfolio.subscribe`.

- `get_current_portfolio_data` создаёт внутреннюю `portfolio.subscribe`,
  возвращает снапшот и сразу вызывает `portfolio.unsubscribe`;
- `subscribe_portfolio` оставляет подписку активной и возвращает
  `subscription_id`;
- `get_portfolio_updates` читает последующие ответы с `r="u"` или новые
  снапшоты с `r="s"`;
- `unsubscribe_portfolio` явно закрывает подписку.

Снапшот возвращается без фильтрации полей: сохраняются все поля текущего
шаблона портфеля, `uf0`…`uf19`, `timetable`, объект `securities` и все
динамические поля каждого инструмента. Обновления также не нормализуются до
заранее заданного набора: частичные изменения пользовательских полей,
инструментов и `__action="del"` возвращаются без потерь.

Ответ содержит служебные поля `type`, `eid`, `ts`, `r`, `data`, а также удобные
алиасы `robot_id`, `portfolio`, `value`, `deleted` и `subscription_id`.
Проверяются обязательные поля ответа, совпадение `r_id/p_id`, имя портфеля и
соответствие ключа инструмента его `sec_key`. Ошибки Viking с `r="e"` содержат
исходные `code`, `msg` и полный API-ответ.

### `get_portfolio_data`

Этот инструмент отвечает только за исторические значения
`portfolio_history.get_history/get_previous`; его имя сохранено для обратной
совместимости.

Обязательные параметры:

- `robot_id`;
- `portfolio`;
- `date_from` и `date_to` в ISO 8601 с часовым поясом.

Необязательные параметры:

- `fields` — по умолчанию `buy`, `sell`, `pos`;
- `aggregation` — `raw`, `10s`, `1m`, `5m`, `10m`, `1h`, `6h`, `24h`;
- `delivery` — `auto`, `inline`, `file`, `stream`, `summary`;
- `preview_rows` — от 0 до 100.

## Локальный запуск

Требуются Python 3.11+ и `uv`.

```bash
uv sync --dev
uv run pytest
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Локальные адреса:

- health: `http://127.0.0.1:8000/health`;
- setup: `http://127.0.0.1:8000/setup`;
- MCP: `http://127.0.0.1:8000/mcp`.

## Railway

Проект содержит `Dockerfile` и `railway.json`. Нужны публичный домен и Volume
с mount path `/data`.

Рекомендуемые переменные:

```dotenv
VIKING_WS_URL=wss://bot.fkviking.com/ws
VIKING_REQUEST_TIMEOUT_SECONDS=45
CREDENTIALS_IDLE_TTL_SECONDS=900
OAUTH_SESSION_IDLE_TTL_SECONDS=900
OAUTH_SESSION_MAX_TTL_SECONDS=28800
OAUTH_PERSISTENT_TOKEN_TTL_SECONDS=2592000
OAUTH_CLIENT_STORE_PATH=/data/oauth-clients.json
EXPORT_DIR=/data/exports
INLINE_MAX_ROWS=500
INLINE_MAX_BYTES=200000
MAX_POINTS_PER_FIELD=500000
EXPORT_TTL_SECONDS=86400
EXPORT_SIGNING_KEY=случайный внутренний секрет
```

`RAILWAY_PUBLIC_DOMAIN` Railway задаёт автоматически. `EXPORT_SIGNING_KEY`
подписывает CSV-ссылки и по умолчанию служит ключом для зашифрованных OAuth-токенов.
Для независимой ротации можно задать отдельный `CREDENTIAL_TOKEN_KEY`.
`OAUTH_CLIENT_STORE_PATH` содержит только технические регистрации MCP-клиентов
(`client_id`, redirect URI и client secret), но не Viking credentials. Этот файл
нужен, чтобы Codex и Claude Code могли повторно авторизоваться после рестарта Railway.

## Безопасность

- OAuth 2.1 с PKCE и динамической регистрацией клиента;
- публичный адрес MCP, но вызовы инструментов требуют пользовательский OAuth;
- каждый пользователь авторизуется своими Viking credentials;
- у Railway нет постоянной базы пользовательских credentials;
- session credentials существуют только в RAM;
- технические регистрации OAuth-клиентов сохраняются на Railway Volume, чтобы
  повторный вход работал после перезапуска сервера;
- local credentials находятся в зашифрованном токене, который хранит MCP-клиент;
- CSV-ссылки подписаны, ограничены по времени и не содержат API key;
- сервер предоставляет только read-only инструменты.

## Примеры запросов

```text
Покажи доступные мне портфели и отметь, у каких включена история.
```

```text
Скачай buy, sell и pos по портфелю demo робота 1
за период с 2026-07-25T10:00:00+03:00 по 2026-07-25T11:00:00+03:00.
Используй агрегацию 1m.
```
