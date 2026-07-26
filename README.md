# Viking Market Data MCP

MVP MCP-сервера поверх WebSocket API `bot.fkviking.com`.

Сервер решает две основные задачи:

1. показывает портфели, доступные текущей роли Viking;
2. выгружает историю выбранного портфеля за заданный период.

Для небольших результатов `get_portfolio_data` возвращает строки непосредственно
в `structuredContent`. Для больших результатов сервер создаёт CSV и возвращает
временную подписанную ссылку. Запрос `delivery=inline` может быть автоматически
переключён на файл, если ответ превышает серверные лимиты.

## MCP tools

### `list_available_portfolios`

Параметры:

- `history_only=false` — вернуть все доступные портфели;
- `history_only=true` — вернуть только портфели с включённой записью истории.

Результат содержит:

- `robot_id`;
- `portfolio`;
- `owner`;
- `history_available`.

### `get_portfolio_data`

Обязательные параметры:

- `robot_id`;
- `portfolio`;
- `date_from` — ISO 8601 с часовым поясом;
- `date_to` — ISO 8601 с часовым поясом.

Необязательные параметры:

- `fields` — по умолчанию `buy`, `sell`, `pos`;
- `aggregation` — `raw`, `10s`, `1m`, `5m`, `10m`, `1h`, `6h`, `24h`;
- `delivery` — `auto`, `inline`, `file`, `stream`, `summary`;
- `preview_rows` — от 0 до 100.

## 1. Модель credentials

Публичный MCP не хранит общие пользовательские credentials. Каждый MCP-клиент
передаёт собственные данные Viking в каждом HTTPS-запросе:

```text
X-Viking-Email
X-Viking-API-Key
X-Viking-Role
```

API key не является аргументом MCP tool и не попадает в запрос модели. Сервер
использует credentials только в оперативной памяти для авторизации постоянного
WebSocket-соединения с Viking. В переменные Railway, базу данных и файлы они не
записываются.

## 2. Локальный запуск через HTTP

Требуется Python 3.11+ и `uv`.

```bash
uv sync --dev
uv run pytest
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Проверка:

```bash
curl http://127.0.0.1:8000/health
```

MCP endpoint:

```text
http://127.0.0.1:8000/mcp
```

## 3. Деплой в Railway

Проект содержит `Dockerfile` и `railway.json`.

Переменные Railway:

```dotenv
VIKING_WS_URL=wss://bot.fkviking.com/ws
EXPORT_DIR=/data/exports
INLINE_MAX_ROWS=500
INLINE_MAX_BYTES=200000
MAX_POINTS_PER_FIELD=500000
EXPORT_TTL_SECONDS=86400
EXPORT_SIGNING_KEY=случайный внутренний секрет для подписания CSV-ссылок
VIKING_REQUEST_TIMEOUT_SECONDS=45
```

В Railway нужно:

1. создать сервис из репозитория или выполнить `railway up`;
2. добавить перечисленные переменные;
3. добавить Volume с mount path `/data`;
4. создать публичный домен;
5. дождаться успешной проверки `/health`.

После выдачи домена MCP URL будет иметь вид:

```text
https://YOUR-DOMAIN.up.railway.app/mcp
```

## 4. Подключение Codex

Credentials задаются локально на компьютере пользователя:

```powershell
setx VIKING_EMAIL "user@example.com"
setx VIKING_API_KEY "личный API key"
setx VIKING_ROLE "trader"
```

После `setx` нужно открыть новое окно терминала.

```toml
[mcp_servers.viking_marketdata]
url = "https://YOUR-DOMAIN.up.railway.app/mcp"
env_http_headers = { "X-Viking-Email" = "VIKING_EMAIL", "X-Viking-API-Key" = "VIKING_API_KEY", "X-Viking-Role" = "VIKING_ROLE" }
tool_timeout_sec = 300
```

Проверьте:

```bash
codex mcp list
```

В интерфейсе Codex используйте `/mcp`.

Любой другой MCP-клиент подключается к тому же публичному URL и должен
передавать эти три HTTP-заголовка из своего безопасного локального хранилища
или переменных окружения.

## 5. Примеры запросов

```text
Покажи доступные мне портфели и отметь, у каких включена история.
```

```text
Скачай buy, sell и pos по портфелю demo робота 1
за период с 2026-07-25T10:00:00+03:00 по 2026-07-25T11:00:00+03:00.
Используй агрегацию 1m.
```

```text
Покажи первые строки истории buy и sell по портфелю demo за последний час.
Если данных много, верни CSV.
```

## Безопасность

- У сервера нет общих Viking email/API key и глобального MCP Bearer-токена.
- Каждый пользователь работает только со своими credentials.
- Credentials передаются по HTTPS-заголовкам, не входят в MCP tool arguments и не записываются на диск.
- CSV-ссылки подписаны отдельным инфраструктурным `EXPORT_SIGNING_KEY`, ограничены по времени и не содержат API key.
- `/health` не раскрывает значения секретов.
- Сервер предоставляет только read-only tools.

## Ограничения MVP

- Поддерживается история полей портфеля, а не сделок и заявок.
- `stream` принимается как значение контракта, но в MVP преобразуется в CSV.
- Для файла используется локальный диск/Volume Railway, а не S3.
- При превышении `MAX_POINTS_PER_FIELD` нужно укрупнить агрегацию или сократить период.
