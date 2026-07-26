# Viking Market Data MCP

Публичный read-only MCP-сервер поверх WebSocket API `bot.fkviking.com`.

Он предоставляет два инструмента:

- `list_available_portfolios` — доступные пользователю портфели;
- `get_portfolio_data` — история выбранного портфеля за период.

Небольшой результат возвращается непосредственно в MCP. Большой результат
сохраняется в CSV и возвращается временной подписанной ссылкой.

## Подключение пользователя

MCP URL:

```text
https://YOUR-DOMAIN.up.railway.app/mcp
```

1. Добавьте этот адрес в Codex как **Потоковая передача HTTP**.
2. Нажмите **Авторизоваться**.
3. В браузере выберите одну из двух кнопок:

   - **Только на эту сессию** — Viking credentials находятся только в RAM
     Railway. Они удаляются после 15 минут без запросов, завершения сессии или
     перезапуска сервера.
   - **Запомнить на этом компьютере** — Codex хранит локальный OAuth-токен.
     Viking credentials зашифрованы внутри токена; Railway не сохраняет их в
     базе данных или файлах.

4. Введите `Email`, `API key` и `Role`, затем нажмите **Подключить**.

Credentials вводятся только на странице авторизации. Они не являются
аргументами MCP-инструментов, не передаются модели и не должны отправляться в чат.

## MCP tools

### `list_available_portfolios`

Параметр `history_only=true` оставляет только портфели, по которым доступна
история. Для каждого результата возвращаются `robot_id`, `portfolio`, `owner`
и `history_available`.

### `get_portfolio_data`

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

## Безопасность

- OAuth 2.1 с PKCE и динамической регистрацией клиента;
- публичный адрес MCP, но вызовы инструментов требуют пользовательский OAuth;
- каждый пользователь авторизуется своими Viking credentials;
- у Railway нет постоянной базы пользовательских credentials;
- session credentials существуют только в RAM;
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
