# AGENTS.md — Viking Market Data MCP

Этот файл является постоянным контекстом проекта для Codex, Claude Code и других
ИИ-агентов. Перед анализом, изменением или публикацией кода прочитайте файл
целиком, затем сверяйте утверждения с актуальным `main`.

## 1. Назначение и текущее состояние

`viking-marketdata-mcp` — публичный многопользовательский read-only MCP-сервер
поверх WebSocket API `bot.fkviking.com`.

Репозиторий:
<https://github.com/Ramizkt/viking-marketdata-mcp>

Официальная документация Viking WebSocket API:
<https://github.com/fkviking/bot-doc/blob/master/assets/ru/api.md>

Production MCP:
<https://viking-marketdata-mcp-production.up.railway.app/mcp>

Служебные адреса:

- health: `https://viking-marketdata-mcp-production.up.railway.app/health`;
- setup: `https://viking-marketdata-mcp-production.up.railway.app/setup`.

В актуальной версии реализованы 42 MCP-инструмента. Сервер
только читает данные: он не создаёт и не изменяет портфели, не меняет поля, не
отправляет торговые сигналы и заявки.

## 2. Источники истины

Для каждой задачи используйте источники в таком порядке:

1. Актуальный код ветки `main` определяет, что сервер реально делает сейчас.
2. Официальный `api.md` определяет контракт Viking WebSocket API.
3. `README.md` определяет пользовательский сценарий подключения и работы.
4. Тесты определяют зафиксированные граничные случаи и обратную совместимость.
5. Описания прошлых PR — только история решений, а не актуальная спецификация.

Нельзя описывать или реализовывать API по памяти. Перед добавлением метода найдите
его точный раздел в `api.md` и проверьте:

- `type`;
- обязательные и необязательные поля запроса;
- единицы времени;
- успешные значения `r`;
- структуру snapshot/update/error;
- правила подписки и отписки.

Примеры в официальной документации могут не содержать все необязательные поля.
Не ограничивайте ответ только полями из примера, если API допускает динамические
или дополнительные поля.

## 3. Что здесь точно не используется

В проекте нет REST-эндпоинтов:

- `GET /api/v1/instruments`;
- `POST /api/v1/market-data/export`.

В проекте нет MCP-инструментов:

- `list_instruments`;
- `download_market_data`;
- `get_data_coverage`.

Это были ошибочно предполагаемые названия из другой схемы и их нельзя выдавать
за текущую реализацию.

Важно: `get_portfolio_data` — имя MCP-инструмента, а не `type` операции Viking
API. История фактически запрашивается через
`portfolio_history.get_history`/`portfolio_history.get_previous`. Текущее
состояние фактически приходит в snapshot операции `portfolio.subscribe`.

## 4. Технологии и структура проекта

Основной стек:

- Python 3.11+; production image использует Python 3.12;
- `mcp`/FastMCP со Streamable HTTP;
- Starlette и Uvicorn;
- `websockets` для постоянного соединения с Viking;
- Pydantic Settings;
- `cryptography`/AES-GCM для self-contained OAuth-токенов;
- `uv` для зависимостей и команд разработки;
- pytest, pytest-asyncio и Ruff;
- Docker и Railway; постоянные файлы находятся на Railway Volume `/data`.

Ключевые файлы:

- `app/main.py` — FastMCP, описания и схемы инструментов, OAuth-защита,
  Starlette routes `/mcp`, `/health`, `/setup`, `/downloads/...`;
- `app/viking_client.py` — WebSocket-соединение, `authorization_key`,
  request/response correlation по `eid`, heartbeat, API-операции, подписки,
  очереди обновлений и проверка протокола;
- `app/service.py` — прикладные операции, проверка параметров истории,
  объединение рядов и выбор inline/file/summary;
- `app/oauth.py` — OAuth 2.1, PKCE, dynamic client registration, браузерная
  форма, режимы хранения credentials и повторная авторизация;
- `app/config.py` — настройки окружения и лимиты;
- `app/export_store.py` — CSV, срок жизни и подписанные download URL;
- `app/onboarding.py` — страница `/setup`;
- `tests/test_viking_client.py` — контракт WebSocket-клиента;
- `tests/test_service.py` — сервисный слой;
- `tests/test_mcp.py` — внешний MCP-контракт;
- `tests/test_oauth.py` — авторизация и повторный вход;
- `tests/test_export_store.py` — CSV и подписанные ссылки;
- `.env.example`, `Dockerfile`, `railway.json`, `README.md` — запуск и deploy;
- `.github/pull_request_template.md` — обязательный checklist каждого PR;
- `.github/workflows/agents-context.yml` — проверка синхронизации кода и `AGENTS.md`.

Архитектурный путь вызова:

`MCP tool -> MarketDataService -> VikingClient -> wss://bot.fkviking.com/ws`.

### Прямое выполнение read-only MCP-запросов

Все инструменты этого MCP являются read-only. Если пользовательская задача может быть
выполнена одним или несколькими инструментами Viking MCP, агент должен вызывать их
напрямую в текущем агенте. Нельзя создавать subagent/дочерний Codex и нельзя запускать
`codex exec`, Terminal, shell или другую локальную команду только как прокси для этих
MCP-вызовов. Такой обход превращает обычное чтение через MCP в shell execution и может
вызвать лишний запрос подтверждения у пользователя.

Это правило не отключает multi-agent глобально: subagent допустим для самостоятельной
аналитической или кодовой работы, но не как обёртка над read-only Viking MCP.

Для каждого набора пользовательских Viking credentials пул переиспользует одно
постоянное WebSocket-соединение. Официальный API ограничивает один API key
16 одновременными соединениями, поэтому нельзя без причины создавать отдельное
соединение на каждый вызов.

## 5. Авторизация и безопасность

Здесь есть два разных уровня авторизации, их нельзя смешивать.

### 5.1. OAuth между MCP-клиентом и этим сервером

1. Codex или Claude Code подключается к `/mcp`.
2. Сервер использует OAuth 2.1 с PKCE и dynamic client registration.
3. Пользователь нажимает штатную кнопку авторизации MCP.
4. В браузере пользователь выбирает режим хранения и вводит `Email`, `API key`
   и `Role`.
5. Credentials проверяются через реальную авторизацию Viking.
6. После успешной проверки authorization code возвращается в loopback callback
   MCP-клиента, затем обменивается на bearer access token.

Credentials:

- не являются аргументами MCP-инструментов;
- не должны попадать в чат, prompt, логи, тестовые фикстуры, issue или PR;
- не должны запрашиваться агентом у пользователя в переписке.

На странице есть два режима:

- **Только на эту сессию**: credentials и session access token хранятся в RAM
  Railway; access token и ротируемый refresh token имеют честный idle TTL по
  умолчанию 15 минут, максимальный срок всей сессии — 8 часов. Refresh token
  также существует только в RAM и при использовании заменяется новым. Рестарт
  сервера или окончательное истечение сессии удаляет оба токена, после чего
  Codex должен однозначно перейти в состояние повторной авторизации.
- **Запомнить на этом компьютере**: credentials находятся внутри
  зашифрованного AES-GCM self-contained OAuth-токена, который хранит MCP-клиент;
  TTL по умолчанию 30 дней; Railway не сохраняет эти Viking credentials в базе
  или пользовательском файле.

`OAUTH_CLIENT_STORE_PATH` (production: `/data/oauth-clients.json`) хранит только
технические регистрации MCP-клиентов: `client_id`, redirect URI и при наличии
client secret. Файл нужен для повторной авторизации после рестарта Railway,
пишется атомарно с правами `0600` и не содержит Viking email/API key.

Нельзя рекламировать session access token как действующий дольше, чем сервер
фактически готов его принимать. Иначе Codex продолжает использовать локально
закэшированный токен, получает `401`, но UI может не показать кнопку повторной
авторизации. Session access token должен истекать через idle TTL, а
краткоживущая сессия должна продлеваться только ротируемым refresh token.

Не возвращайте CSP-ограничение `form-action 'self'`: оно блокирует переход с
Railway на loopback OAuth callback Codex/Claude.

### 5.2. Авторизация этого сервера в Viking

`VikingClient` открывает `wss://bot.fkviking.com/ws` и отправляет:

- `type="authorization_key"`;
- `data.email`;
- `data.key`;
- `data.role`;
- служебный `eid`.

В текущем клиенте используются `group=0.1` и `compress=true`. После успешного
ответа соединение используется для дальнейших запросов. Не добавляйте общие
Viking credentials в Railway environment и не возвращайте старую схему ручных
`X-Viking-*` заголовков: актуальный пользовательский flow — браузерный OAuth.

### 5.3. Инфраструктурные секреты

- `EXPORT_SIGNING_KEY` подписывает временные CSV-ссылки и по умолчанию является
  основой ключа шифрования remembered OAuth tokens.
- `CREDENTIAL_TOKEN_KEY` можно задать отдельно для независимой ротации токенов.
- Если оба ключа пусты, remembered tokens перестанут работать после рестарта.
- Секреты никогда не коммитятся; `.env` и `data/` игнорируются Git.

## 6. Реально используемые методы Viking WebSocket API

Сейчас реализовано 35 типов операций Viking:

| Viking API `type` | Назначение | Где используется |
|---|---|---|
| `authorization_key` | Авторизация WebSocket-соединения | автоматически перед остальными вызовами |
| `available_portfolio_list.subscribe` | Snapshot и обновления доступных портфелей | list/subscribe available portfolios |
| `available_portfolio_list.unsubscribe` | Завершение подписки на список | list/unsubscribe available portfolios |
| `available_portfolio_list.get_with_history` | Портфели с включённой записью истории | `list_available_portfolios` |
| `get_template_id` | ID шаблона объекта `view="portfolio"` по `{r_id,p_id}` | `get_portfolio_template` |
| `get_template_by_id` | Полный шаблон по `template_id` | `get_portfolio_template` |
| `portfolio.subscribe` | Snapshot и обновления текущего состояния портфеля | current/subscribe portfolio |
| `portfolio.unsubscribe` | Завершение подписки на портфель | current/unsubscribe portfolio |
| `portfolio_logs.subscribe` | Snapshot и новые записи логов портфеля | subscribe/get portfolio log updates |
| `portfolio_logs.unsubscribe` | Завершение подписки на логи портфеля | unsubscribe portfolio logs |
| `robot_logs.subscribe` | Snapshot и новые записи логов робота | subscribe/get robot log updates |
| `robot_logs.unsubscribe` | Завершение подписки на логи робота | unsubscribe robot logs |
| `robot_logs.get_history` | История логов робота в диапазоне `epoch_nsec` | `get_robot_log_history` |
| `portfolio_deals.subscribe` | Snapshot и новые сделки портфеля по инструментам | subscribe/get portfolio deal updates |
| `portfolio_deals.unsubscribe` | Завершение подписки на сделки | unsubscribe portfolio deals |
| `portfolio_deals.get_previous` | До 100 сделок старше `mt` | `get_previous_portfolio_deals` |
| `portfolio_deals.get_sec_keys` | Уникальные инструменты в истории сделок | `get_portfolio_deal_sec_keys` |
| `portfolio_deals.get_history` | Сделки во включительном диапазоне `epoch_nsec` | `get_portfolio_deal_history` |
| `data_conn.subscribe` | Snapshot и обновления market-data подключений | subscribe/get data connection updates |
| `data_conn.get_all` | Список market-data подключений робота | `get_all_data_connections` |
| `data_conn.unsubscribe` | Завершение подписки на market-data подключения | `unsubscribe_data_connections` |
| `trans_conn.get` | Параметры transactional connection | `get_transaction_connection` |
| `trans_conn.get_used_secs` | Инструменты портфелей с client code подключения | `get_transaction_connection_used_securities` |
| `trans_conn.subscribe` | Snapshot и обновления transactional connections | subscribe/get transaction connection updates |
| `trans_conn.get_all` | Список transactional connections | `get_all_transaction_connections` |
| `trans_conn.unsubscribe` | Завершение подписки на transactional connections | `unsubscribe_transaction_connections` |
| `trans_conn_orders.subscribe` | Snapshot и обновления активных заявок | subscribe/get transaction order updates |
| `trans_conn_orders.unsubscribe` | Завершение подписки на заявки | `unsubscribe_transaction_orders` |
| `trans_conn_poses.subscribe` | Snapshot и обновления позиций | subscribe/get transaction position updates |
| `trans_conn_poses.unsubscribe` | Завершение подписки на позиции | `unsubscribe_transaction_positions` |
| `robot.get_securities` | Полный, возможно многостраничный список инструментов робота | `get_robot_securities` |
| `robot.get_client_codes` | Клиентские коды робота с `sec_type` и `ll` | `get_robot_client_codes` |
| `robot.find_security` | Поиск SecKey в портфелях и формулах | `find_security` |
| `portfolio_history.get_history` | Первая порция истории поля в диапазоне | `get_portfolio_data` |
| `portfolio_history.get_previous` | Пагинация к более ранним точкам | `get_portfolio_data` |

Общие служебные поля ответа следует сохранять: `type`, `eid`, `ts`, `r`,
`data`. Основные коды результата:

- `r="p"` — успешный одноразовый запрос или подтверждение;
- `r="s"` — snapshot подписки;
- `r="u"` — update подписки;
- `r="e"` — ошибка; сохраняйте исходные `data.code`, `data.msg` и полный ответ.

Повреждённый ответ, несовпадение `eid`, `r_id`/`p_id`, неожиданный `r`, потеря
соединения или переполнение очереди не должны превращаться в успешный результат.

Особенности методов инструментов:

- `robot.get_securities` может вернуть несколько `r="p"` с одним `eid`;
  `data.next=true` означает, что нужно дождаться следующего сообщения, а не
  завершать вызов. Объединяйте страницы по ключу `SEC_KEY` и проверяйте его
  равенство внутреннему `sec_key`;
- `reload=false` использует backend-кэш, `reload=true` запрашивает данные заново
  из робота; `sec_type` является необязательной числовой битовой маской;
- у `robot.get_client_codes` ответ `values` — массив объектов
  `{sec_type, ll}`, несмотря на промежуточную строку `object` в таблице;
- `robot.find_security` требует `key`, а необязательные `r_id` и `p_id` сужают
  область. Возвращайте без потерь и `portfolios`, и `formulas`: последние
  содержат точное место вхождения SecKey в поле или формулу.
После потери соединения активная подписка не восстанавливается молча: создайте
новую.

В официальном `api.md` есть две опечатки, которые нельзя переносить в код:

- таблица запроса `trans_conn.get_used_secs` ошибочно пишет `trans_conn.get`;
  wire-type подтверждён примером и ответами как `trans_conn.get_used_secs`;
- раздел ошибки `trans_conn.subscribe` ошибочно пишет `trans_conn.get_all`;
  подписка использует `trans_conn.subscribe`.

## 7. Реально доступные MCP-инструменты

### 7.1. Список доступных портфелей

`list_available_portfolios(history_only=false)`

Внутренняя последовательность:

1. `available_portfolio_list.subscribe`;
2. получение snapshot;
3. `available_portfolio_list.unsubscribe`;
4. `available_portfolio_list.get_with_history`;
5. добавление `history_available` к каждому портфелю.

Результат содержит `robot_id`, `portfolio`, `owner`, `history_available`.
`history_only=true` оставляет только портфели с доступной историей.

### 7.2. Подписка на изменения доступных портфелей

- `subscribe_available_portfolios()` возвращает snapshot и `subscription_id`;
- `get_available_portfolio_updates(subscription_id, wait_seconds=0,
  max_events=100)` возвращает `portfolios_add`/`portfolios_del`;
- `unsubscribe_available_portfolios(subscription_id)` завершает подписку.

`wait_seconds` допустим от 0 до 30, `max_events` — от 1 до 500. Эта подписка
возвращает только `robot_id`, `portfolio`, `owner`, а не `buy`, `sell`, `pos`,
`uf0...uf19` или другие поля портфеля.

### 7.3. Шаблон портфеля

`get_portfolio_template(robot_id, portfolio)`

Это высокоуровневая обязательная цепочка:

1. `get_template_id(view="portfolio", object_id={"r_id": ..., "p_id": ...})`;
2. `get_template_by_id(template_id=...)`.

Возвращайте полный `template` и все разделы `template_fields` без фильтрации,
включая `portfolio`, `security`, `timetable`, `notifications` и неизвестные
заранее дополнительные разделы. Шаблон нужен для интерпретации динамических
полей, типов и допустимых диапазонов.

### 7.4. Текущее состояние портфеля

`get_current_portfolio_data(robot_id, portfolio)` создаёт внутреннюю
`portfolio.subscribe`, получает полный snapshot и сразу вызывает
`portfolio.unsubscribe`.

Snapshot сохраняется без фильтрации:

- стандартные и динамические поля портфеля;
- `uf0...uf19`;
- `timetable`;
- `securities`;
- все динамические поля инструментов.

Критическое правило маршрутизации: для любого вопроса о том, торгуется ли портфель,
включена/выключена ли торговля, активна ли торговля или запущена ли торговля, всегда
используйте `get_robot_portfolio_trading_status`. Не используйте для определения этого
состояния `get_current_portfolio_data` и не интерпретируйте поле `disabled` как статус
торговли, даже если оно уже присутствует в snapshot. `disabled` — только статус состояния
портфеля; проверять его следует только если пользователь явно спрашивает состояние
`disabled`/`enabled` самого портфеля. Торговый статус определяется только по
`robot.subscribe.value.re[].re`: `true` означает, что включён `re_sell` или `re_buy`.

### 7.5. Подписка на изменения одного портфеля

- `subscribe_portfolio(robot_id, portfolio)` возвращает snapshot и
  `subscription_id`;
- `get_portfolio_updates(subscription_id, wait_seconds=0, max_events=100)`
  возвращает накопленные partial updates;
- `unsubscribe_portfolio(subscription_id)` завершает подписку.

Сохраняйте partial updates как есть. `__action="del"` означает удаление поля,
инструмента или портфеля. Не заменяйте динамический ответ заранее заданной
Pydantic-схемой, которая потеряет неизвестные поля. При создании подписки всегда
планируйте отписку в `finally`.

### 7.6. История портфеля

`get_portfolio_data(...)` принимает:

- обязательные `robot_id`, `portfolio`, `date_from`, `date_to`;
- `fields`, по умолчанию `["buy", "sell", "pos"]`;
- `aggregation`: `raw`, `10s`, `1m`, `5m`, `10m`, `1h`, `6h`, `24h`;
- `delivery`: `auto`, `inline`, `file`, `stream`, `summary`;
- `preview_rows`: 0..100.

Поддерживаемые исторические поля:

`sell`, `buy`, `lim_s`, `lim_b`, `price_s`, `price_b`, `pos`, `fin_res`,
`uf0...uf19`.

Правила:

- даты должны быть ISO 8601 и обязательно содержать часовой пояс;
- `date_from < date_to`;
- Viking принимает `mint`, `maxt`, `mt` в epoch milliseconds;
- `portfolio_history.get_history` получает первую часть;
- `portfolio_history.get_previous` вызывается столько раз, сколько нужно для
  более ранних данных, но применяется `MAX_POINTS_PER_FIELD`;
- ряды полей объединяются по timestamp и переносят последнее известное значение;
- специальное отсутствующее значение удаляет поле из текущего состояния;
- `auto` возвращает небольшой результат inline, большой — CSV;
- принудительный `inline` также переключается на CSV при превышении лимитов;
- `stream` пока не реализован отдельно и возвращает файл;
- CSV-ссылка подписана и ограничена `EXPORT_TTL_SECONDS`.

История будет непустой только если для портфеля включена её запись. Поэтому перед
выгрузкой проверяйте `history_available=true`.

### 7.7. Логи портфеля и робота

Подписка на логи портфеля:

- `subscribe_portfolio_logs(robot_id, portfolio)` отправляет
  `portfolio_logs.subscribe` с `data={r_id,p_id}` и возвращает snapshot;
- `get_portfolio_log_updates(subscription_id, wait_seconds=0, max_events=100)`
  возвращает накопленные updates;
- `unsubscribe_portfolio_logs(subscription_id)` отправляет
  `portfolio_logs.unsubscribe` с `data.sub_eid`.

Подписка на логи робота:

- `subscribe_robot_logs(robot_id)` отправляет `robot_logs.subscribe`;
- `get_robot_log_updates(subscription_id, wait_seconds=0, max_events=100)`
  возвращает накопленные updates;
- `unsubscribe_robot_logs(subscription_id)` отправляет
  `robot_logs.unsubscribe` с `data.sub_eid`.

Обе подписки используют `r="s"` для snapshot и `r="u"` для обновлений.
Snapshot содержит `mt` в `epoch_nsec`; внешние `r_id` и `p_id` проверяются
против параметров подписки. Записи сохраняются без фильтрации дополнительных
полей. Обязательны `level`, `msg`, `t`, `dt`; `owner` может быть строкой, пустой
строкой или `null`. Поля `id`, `r_id` и `name` проверяются, когда присутствуют:
официальные таблицы называют некоторые из них обязательными, но JSON-примеры
updates их не содержат.

В официальной таблице запроса `portfolio_logs.subscribe` пропущен `p_id`, хотя
он есть в request example и обязателен во всех snapshot/update response.
Реализация отправляет оба идентификатора: `r_id` и `p_id`.

`get_robot_log_history(robot_id, date_from, date_to, message_filter=None,
limit=100000)`:

- вызывает `robot_logs.get_history`;
- требует ISO 8601 даты с часовым поясом и `date_from < date_to`;
- преобразует границы точно, без float, в строки `mint`/`maxt` `epoch_nsec`;
- принимает `message_filter` длиной до 256 символов, где `*` — любое число
  символов, `.` — один символ;
- принимает `limit` от 1 до 100000;
- требует совпадение `r_id` каждой записи с запрошенным роботом;
- сохраняет дополнительные поля без фильтрации;
- принимает `dt` как integer или digit string, потому что таблица и официальный
  JSON-пример расходятся по JSON-типу.

Viking фильтрует видимые логи по автору записи и авторизованным email/role.
При удалении портфеля или робота сервер Viking автоматически отписывает клиента.
Локальный буфер ограничен 1000 сообщениями; overflow означает потерю данных и
требует новой подписки. После disconnect подписки не восстанавливаются молча.

### 7.8. Сделки портфеля по инструментам

- `subscribe_portfolio_deals(robot_id, portfolio)` отправляет
  `portfolio_deals.subscribe` с `r_id+p_id` и возвращает snapshot;
- `get_portfolio_deal_updates(...)` читает `r="u"` из контролируемого буфера;
- `unsubscribe_portfolio_deals(subscription_id)` передаёт исходный eid как
  `data.sub_eid`;
- `get_previous_portfolio_deals(..., before, security_key=None, limit=100)`
  преобразует `before` в строку `epoch_nsec`; допустимый limit — `1..100`;
- `get_portfolio_deal_sec_keys(...)` возвращает уникальные `sec_key`;
- `get_portfolio_deal_history(..., date_from, date_to, security_key=None,
  limit=100000)` передаёт включительные `mint/maxt` строками `epoch_nsec`.

Каждая сделка сохраняет атрибуты `id`, `ono`, `price`, `orig_price`,
`buy_sell`, `quantity`, `cn`, `sec`, `decimals`, `dt`, `curpos`, `lot_size`
и любые неизвестные поля. `r_id`/`name` проверяются, если они присутствуют:
update в официальной документации их не содержит. Поле `ono` принимается строкой
или integer: таблица описывает string, а официальный snapshot example содержит
number. Фильтр `security_key` передаётся в Viking как `sec_key`.

Поле `aggr` в фактических ответах имеет специальный смысл, хотя во вложенной
версии официального `api.md` оно не перечислено в таблице полей сделки:

- `aggr=false` — обычная запись о биржевой сделке; `price` можно читать как точную
  цену этой сделки;
- `aggr=true` — синтетическая агрегированная запись о реально прошедшей сделке или
  нескольких последовательных сделках. Её `price` равна цене исходной заявки и
  **не является точной ценой отдельного исполнения**.

Критическое правило для агентов: `aggr=true` — это не основание исключать запись.
Такие записи нужно учитывать наравне с `aggr=false` во всех подсчётах и расчётах,
включая число сделок, `quantity`, объём, направления buy/sell, позиции и общую
статистику. Запросы «все сделки», «посчитать сделки» или «проанализировать сделки»
всегда относятся к обоим значениям `aggr`; запрещено молча заменять их запросом
«найти фактические сделки (`aggr=false`)».

Ограничение относится только к точности `price`. В ценозависимых расчётах запись
`aggr=true` также нельзя отбрасывать: используйте доступную цену исходной заявки и
явно помечайте результат как оценочный, не выдавая её за точную цену исполнения.
Фактическая цена исполнения могла быть лучше. Агрегат может появиться, когда ответ
биржи по заявке приходит раньше события сделки.

### 7.9. Подключения, заявки и позиции

- Market-data подключения определяются сервером/колокацией робота и выдаются
  как есть. Некоторые источники используются парой, например Definitions
  вместе с OrderBook или BestPrices.
- Transactional connections пользователь добавляет из доступных типов; обычно
  существует хотя бы `0_virtual`.
- `can_check_pos=true` означает доступность активных заявок, `has_pos=true` —
  доступность позиций. Не все подключения поддерживают эти данные.
- Четыре lifecycle подписок: data connections, transaction connections,
  transaction orders и transaction positions. Каждый состоит из
  `subscribe_*`, `get_*_updates`, `unsubscribe_*`.
- Viking может в любой момент прислать повторный snapshot `r="s"`; reader
  принимает и `r="s"`, и `r="u"`.
- Отписка всегда передаёт исходный subscription eid как `data.sub_eid`.
- Неизвестные биржевые поля сохраняются. Удаления подключений, заявок и позиций
  передаются через `__action="del"`.

## 8. Как ИИ должен пользоваться MCP

Безопасная последовательность:

1. Вызвать `list_available_portfolios`.
2. Не просить у пользователя email или API key в чате.
3. Для понимания полей вызвать `get_portfolio_template`.
4. Для одного текущего snapshot вызвать `get_current_portfolio_data`.
5. Для длительного наблюдения вызвать `subscribe_*`, периодически читать
   `get_*_updates` и обязательно вызвать соответствующий `unsubscribe_*`.
6. Для логов выбрать уровень: portfolio logs для одного портфеля, robot logs для
   всего робота; по окончании обязательно отписаться.
7. Для истории логов робота использовать `get_robot_log_history` и при
   необходимости маску `message_filter`.
8. Для сделок арбитражного портфеля сначала при необходимости получить список
   инструментов через `get_portfolio_deal_sec_keys`, затем запрашивать историю
   с `security_key` или без него.
9. Для истории портфеля проверить `history_available`, использовать даты с часовым поясом
   и по умолчанию `delivery=auto`.

Примеры корректных неоднотипных запросов:

- «Покажи все доступные портфели и отметь, где включена история».
- «Получи шаблон портфеля `demo` робота `1` и объясни поля `uf1` и `uf2`».
- «Сопоставь текущий snapshot портфеля с описанием полей его шаблона».
- «Следи за изменениями `pos` и `fin_res`, затем корректно заверши подписку».
- «Следи за новыми ошибками в логах портфеля и затем корректно отпишись».
- «Покажи историю логов робота за час, оставив сообщения по маске `*error*`».
- «Выгрузи `buy`, `sell`, `pos` за сутки с агрегацией `5m`».
- «Проверь, появился ли новый доступный портфель».

## 9. Обязательный процесс разработки: 8 этапов

Любое расширение MCP выполняется в восемь этапов:

1. **Актуализация** — прочитать этот файл, обновить локальный `main`, проверить
   рабочее дерево и последние merged PR.
2. **Контракт API** — открыть точный раздел официального `api.md`, выписать
   request, response, result codes, timestamps, ошибки и lifecycle подписки.
3. **Gap analysis** — поиском по актуальному коду подтвердить, что метод ещё не
   реализован или точно определить существующее поведение. Не доверять памяти.
4. **Проектирование** — отдельно определить низкоуровневые методы
   `VikingClient`, высокоуровневую операцию сервиса и понятный MCP-инструмент.
   Если официальный flow многошаговый, MCP должен по возможности скрывать эту
   механику одним безопасным инструментом.
5. **Реализация клиента** — добавить payload, correlation, строгую проверку
   ответа, API errors, protocol errors, reconnect/subscription lifecycle и
   сохранение динамических полей.
6. **Сервис и MCP-контракт** — добавить метод `MarketDataService`, типизированные
   аргументы, read-only annotations, понятное описание и безопасный результат.
7. **Проверка и документация** — добавить тесты клиента, сервиса и MCP, при
   необходимости OAuth/export regression; обновить `README.md` и этот файл;
   выполнить `uv run ruff check .` и `uv run pytest -q`.
8. **Публикация** — создать ветку `agent/<short-description>`, закоммитить только
   относящиеся к задаче файлы, заполнить `.github/pull_request_template.md` и
   открыть draft PR с описанием API-контракта и проверок. Не сливать PR и не
   считать Railway обновлённым без явной команды пользователя.

После явной команды на production:

1. убедиться, что head PR не изменился, конфликтов и незакрытых замечаний нет;
2. перевести PR в ready при необходимости;
3. выполнить согласованный merge;
4. проверить обновление Railway и `/health`;
5. отдельно проверить `tools/list` или целевой MCP-вызов — merge в `main` сам по
   себе не доказывает, что runtime уже пересобран.

В репозитории есть один процессный GitHub Actions workflow:
`.github/workflows/agents-context.yml`. Он запускается для pull request и требует
изменить `AGENTS.md` в том же PR, если затронуты `app/`, `.github/`, `Dockerfile`,
`railway.json`, `pyproject.toml`, `uv.lock` или `.env.example`.

Проверка подтверждает только наличие изменения `AGENTS.md`, но не его качество.
Обновляйте содержание по существу: новые методы, инструменты, аргументы, ошибки,
архитектурные решения, авторизацию, эксплуатацию и ограничения. Формальное
изменение пробела или даты не считается корректной актуализацией контекста.

Этот workflow не запускает Ruff или pytest и не является полным CI. Нельзя
писать, что тесты или CI прошли, если выполнялась только проверка
`AGENTS.md consistency` или только локальные команды.

## 10. Тестирование и правила кода

Основные команды:

```bash
uv sync --dev
uv run ruff check .
uv run pytest -q
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Настройки Ruff:

- line length 110;
- target Python 3.11;
- правила `E`, `F`, `I`, `UP`, `B`, `SIM`.

Для каждой новой Viking API-операции тестируйте минимум:

- точный request payload и `type`;
- `eid` correlation;
- успешный ответ;
- `r="e"` с сохранением `code`/`msg`;
- отсутствующие или неверного типа обязательные поля;
- неожиданный `r`;
- несовпадение `r_id`, `p_id`, `sec_key` или других identity fields;
- динамические дополнительные поля без потерь;
- timeout/disconnect;
- snapshot/update/unsubscribe и overflow для подписок;
- внешний MCP tool schema и read-only annotation.

Сохраняйте async-архитектуру и type hints. Не выполняйте крупный рефакторинг
одновременно с добавлением API-метода без отдельного согласования.

## 11. Deploy и эксплуатация

Production развёрнут в Railway из Dockerfile:

- Uvicorn слушает `${PORT}`;
- healthcheck: `/health`;
- restart policy: `ON_FAILURE`, максимум 5 повторов;
- Volume mount: `/data`;
- exports: `/data/exports`;
- OAuth client store: `/data/oauth-clients.json`.

Ключевые переменные перечислены в `.env.example` и `README.md`. Не дублируйте
значения вручную в коде. При изменении OAuth, экспорта или путей проверьте
совместимость существующего Railway Volume.

Поддержка проекта включает:

- сохранение обратной совместимости имён MCP-инструментов;
- строгую диагностику Viking API и protocol errors;
- очистку истёкших RAM-сессий, WebSocket-клиентов и CSV;
- сохранение DCR-регистраций после рестартов;
- проверку повторной авторизации Codex и Claude Code;
- актуализацию README и `AGENTS.md` при каждом изменении возможностей.

## 12. История развития: исходный MVP + 10 merged PR

Исходный MVP создал read-only сервер, список портфелей, историческую выгрузку,
CSV delivery, тесты, Docker/Railway.

Далее выполнено десять этапов:

1. PR #1 — персональные credentials вместо общих серверных credentials.
2. PR #2 — два локальных onboarding-режима; позднее заменены OAuth flow.
3. PR #3 — OAuth 2.1 + PKCE + dynamic client registration и браузерный ввод
   credentials; удалены PowerShell onboarding и ручные заголовки.
4. PR #4 — исправлен redirect на loopback callback Codex/Claude.
5. PR #5 — lifecycle подписки на список доступных портфелей.
6. PR #6 — DCR-регистрации сохраняются на Railway Volume; повторная авторизация
   работает после потери RAM-сессии и рестарта.
7. PR #7 — snapshot и подписка на полное текущее состояние портфеля.
8. PR #8 — цепочка `get_template_id -> get_template_by_id` и
   `get_portfolio_template`.
9. PR #9 — корневой `AGENTS.md` с постоянным контекстом проекта и обязательным
   процессом разработки.
10. PR #10 — PR template и обязательная проверка `AGENTS.md consistency`.

Следующее расширение добавляет `portfolio_logs.subscribe/unsubscribe`,
`robot_logs.subscribe/unsubscribe/get_history` и семь MCP-инструментов полного
lifecycle логов. До merge соответствующего PR production эти возможности не
получает.

Расширение после логов добавляет пять операций `portfolio_deals.*` и шесть
MCP-инструментов для snapshot/update lifecycle, пагинации к прошлому, списка
инструментов и истории сделок за период.

История полезна для понимания решений, но устаревшие механизмы PR #1/#2 нельзя
возвращать без отдельного архитектурного решения.

## 13. Ограничения и запреты

- Сохраняйте сервер read-only, пока пользователь явно не согласовал расширение
  scope и отдельную модель безопасности для write/trading операций.
- Не коммитьте реальные credentials, токены, `.env`, exports или Railway data.
- Не просите API key в чате и не добавляйте его в tool arguments.
- Не выдумывайте API-методы и не переносите сюда контракты других проектов.
- Не называйте MCP tool именем метода Viking API без проверки фактической цепочки.
- Не фильтруйте неизвестные поля template/snapshot/update.
- Не оставляйте подписки открытыми без необходимости.
- Не утверждайте, что deploy завершён, пока не проверен runtime.
- Не выполняйте merge, deploy, write-операции Viking или изменение секретов без
  явного разрешения пользователя.
- Не изменяйте одновременно посторонние файлы и не включайте чужие изменения в
  commit/PR.

## 14. Definition of done

Задача завершена только когда:

- API-контракт подтверждён официальной документацией;
- реализация есть на уровнях client/service/MCP, где это применимо;
- errors и dynamic fields не теряются;
- добавлены необходимые regression tests;
- Ruff и pytest прошли либо честно описан конкретный blocker;
- README и `AGENTS.md` соответствуют коду;
- заполнен PR checklist, а `AGENTS.md consistency` прошла, если она применима;
- создан отдельный draft PR;
- merge/deploy выполнены только по явной команде;
- production runtime проверен отдельно после deploy.


## Response contract v2

- Никогда не возвращать один массив одновременно внутри сырого `data.values` и в нормализованном поле. Канонический массив — `items`; raw допускается только по `raw=true`.
- Пустой результат обязан иметь `data_status` и человекочитаемую причину в `notes`.
- Viking sentinel `-9007199254740992` трактуется как отсутствие значения и возвращается как `null`. Не считать `2147483647` sentinel без подтверждения документацией: `d_pg` документирован как pagination data.
- Для epoch-nanoseconds сохранять исходное поле и добавлять `_iso` в запрошенной timezone.
- Полные JSON-снапшоты в `msg` не выдавать при compact-режиме. Не выдумывать before/after: если Viking пишет только снапшот, явно указывать `diff_available=false`.
- Новые агрегаты по роботу нельзя заявлять как быстрые, пока нет измеренного серверного кэша/параллельного плана и приёмочного теста.

- Fast-path invariant: `get_current_portfolio_data` must request the portfolio snapshot directly and must not call `list_portfolios()` before a successful read. The portfolio list may be fetched only after `VikingAPIError` to enrich an accessible-robot `portfolio_not_found` diagnostic; otherwise preserve the original Viking API error.


## Robot portfolio trading aggregate

- `get_robot_portfolio_trading_status` — канонический инструмент для вопросов «торгуется / не торгуется».
- Единственный источник per-portfolio статуса — `robot.subscribe` -> `value.re`; документация определяет `re[].n` как имя портфеля, `re[].re` как `Is re_sell or re_buy`.
- `re=true` -> `trading`, `re=false` -> `not_trading`.
- Не использовать `portfolio.disabled`, `p_d` или `p_a - p_d` для определения торговли.
- Не делать `available_portfolio_list.subscribe` или fan-out `portfolio.subscribe`: массив `re` всегда приходит целиком в одном snapshot.
- После snapshot выполнить `robot.unsubscribe`.
