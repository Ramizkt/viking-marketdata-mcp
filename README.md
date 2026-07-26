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

### `credential_setup`

Возвращает безопасную страницу настройки и два режима credentials. Инструмент
доступен до авторизации, поэтому публичный MCP можно сначала просто добавить
в клиент.

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
записываются. Неактивная копия удаляется из RAM через
`CREDENTIALS_IDLE_TTL_SECONDS` (по умолчанию 900 секунд).

Пользователь выбирает один из двух локальных режимов:

1. **Текущая сессия** — credentials вводятся скрыто в PowerShell и существуют
   только в памяти процессов.
2. **Зашифрованный локальный файл** — email и role хранятся локально, API key
   шифруется Windows DPAPI. Файл читает launcher, а не модель.

Страница настройки:

```text
https://YOUR-DOMAIN.up.railway.app/setup
```

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
VIKING_REQUEST_TIMEOUT_SECONDS=45
CREDENTIALS_IDLE_TTL_SECONDS=900
EXPORT_DIR=/data/exports
INLINE_MAX_ROWS=500
INLINE_MAX_BYTES=200000
MAX_POINTS_PER_FIELD=500000
EXPORT_TTL_SECONDS=86400
EXPORT_SIGNING_KEY=случайный внутренний секрет для подписания CSV-ссылок
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

## 4. Подключение Codex App на Windows

Сначала добавьте публичный MCP URL в **Settings → MCP servers → Add server**:

```toml
[mcp_servers.viking_marketdata]
url = "https://YOUR-DOMAIN.up.railway.app/mcp"
env_http_headers = { "X-Viking-Email" = "VIKING_EMAIL", "X-Viking-API-Key" = "VIKING_API_KEY", "X-Viking-Role" = "VIKING_ROLE" }
tool_timeout_sec = 300
```

Без credentials подключение всё равно успешно: Codex увидит
`credential_setup`, `list_available_portfolios` и `get_portfolio_data`.
При первом запросе агент должен предложить два режима и дать ссылку `/setup`.

### Режим 1: только текущая сессия

1. Полностью закройте Codex App.
2. Скачайте `/client/windows/viking-session.ps1`.
3. Запустите:

```powershell
powershell -ExecutionPolicy Bypass -File .\viking-session.ps1
```

Скрипт скрыто запросит credentials, запишет MCP-конфигурацию и запустит
`codex app`. В реестр и файлы credentials не записываются.

### Режим 2: зашифрованный локальный файл

1. Скачайте в одну папку `save-viking-credentials.ps1`,
   `viking-file.ps1` и `viking-session.ps1`.
2. Один раз создайте файл:

```powershell
powershell -ExecutionPolicy Bypass -File .\save-viking-credentials.ps1
```

3. Затем запускайте Codex так:

```powershell
powershell -ExecutionPolicy Bypass -File .\viking-file.ps1
```

По умолчанию файл находится в `%USERPROFILE%\.viking-mcp\credentials.json`.
API key зашифрован DPAPI и доступен только текущей учётной записи Windows.

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
- Публичный MCP и список инструментов доступны без credentials; сами запросы к Viking без них отклоняются.
- Неактивные credentials удаляются из RAM Railway через заданный TTL.
- CSV-ссылки подписаны отдельным инфраструктурным `EXPORT_SIGNING_KEY`, ограничены по времени и не содержат API key.
- `/health` не раскрывает значения секретов.
- Сервер предоставляет только read-only tools.

## Ограничения MVP

- Поддерживается история полей портфеля, а не сделок и заявок.
- `stream` принимается как значение контракта, но в MVP преобразуется в CSV.
- Для файла используется локальный диск/Volume Railway, а не S3.
- При превышении `MAX_POINTS_PER_FIELD` нужно укрупнить агрегацию или сократить период.
