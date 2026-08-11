# Viking Market Data MCP

Публичный read-only MCP-сервер поверх WebSocket API `bot.fkviking.com`.

Он предоставляет 42 инструмента:

- `list_available_portfolios` — доступные пользователю портфели;
- `subscribe_available_portfolios` — подписка на изменения списка портфелей;
- `get_available_portfolio_updates` — чтение накопленных событий подписки;
- `unsubscribe_available_portfolios` — явное завершение подписки;
- `get_portfolio_template` — полная схема полей выбранного портфеля;
- `get_current_portfolio_data` — полный текущий снапшот выбранного портфеля;
- `subscribe_portfolio` — подписка на все изменения выбранного портфеля;
- `get_portfolio_updates` — чтение обновлений полей портфеля и его инструментов;
- `unsubscribe_portfolio` — явное завершение подписки на портфель;
- `subscribe_portfolio_logs` — подписка на новые записи логов портфеля;
- `get_portfolio_log_updates` — чтение накопленных логов портфеля;
- `unsubscribe_portfolio_logs` — завершение подписки на логи портфеля;
- `subscribe_robot_logs` — подписка на новые записи логов робота;
- `get_robot_log_updates` — чтение накопленных логов робота;
- `unsubscribe_robot_logs` — завершение подписки на логи робота;
- `get_robot_log_history` — история логов робота за период;
- `subscribe_portfolio_deals` — подписка на новые сделки по инструментам портфеля;
- `get_portfolio_deal_updates` — чтение накопленных новых сделок;
- `unsubscribe_portfolio_deals` — завершение подписки на сделки;
- `get_previous_portfolio_deals` — до 100 сделок старше указанной даты;
- `get_portfolio_deal_sec_keys` — уникальные инструменты из истории сделок;
- `get_portfolio_deal_history` — история сделок за период с фильтром по инструменту;

В ответах со сделками `aggr=true` — это такая же сделка, как запись с
`aggr=false`. Её нельзя отбрасывать: она обязательно учитывается при подсчёте
сделок, количества, объёма, направлений buy/sell и в любых других общих расчётах.
Запрос «все сделки» всегда включает оба значения `aggr` и не подразумевает
автоматический фильтр `aggr=false`.

Отличается только точность цены. Для `aggr=true` поле `price` содержит цену
исходной заявки, а не точную цену отдельного исполнения. В ценозависимых расчётах
такую запись тоже нужно учитывать, используя доступную `price`, но результат
следует явно помечать как оценочный: фактическая цена исполнения могла быть лучше.

- `subscribe_data_connections`, `get_data_connection_updates`,
  `unsubscribe_data_connections` — статусы market-data подключений;
- `get_all_data_connections` — полный список market-data подключений робота;
- `get_transaction_connection` — параметры transactional connection;
- `get_transaction_connection_used_securities` — инструменты портфелей,
  относящиеся к выбранному transactional connection;
- `subscribe_transaction_connections`, `get_transaction_connection_updates`,
  `unsubscribe_transaction_connections` — статусы transactional connections;
- `get_all_transaction_connections` — полный список transactional connections;
- `subscribe_transaction_orders`, `get_transaction_order_updates`,
  `unsubscribe_transaction_orders` — активные заявки подключения;
- `subscribe_transaction_positions`, `get_transaction_position_updates`,
  `unsubscribe_transaction_positions` — позиции подключения;
- `get_robot_securities` — все доступные роботу финансовые инструменты с
  фильтром по битовой маске `sec_type` и опциональным принудительным reload;
- `get_robot_client_codes` — доступные роботу клиентские коды и их `sec_type`;
- `find_security` — поиск точного SecKey в портфеле, роботе или во всех
  доступных роботах, включая вхождения в формулах;
- `get_portfolio_data` — история выбранного портфеля за период.

Небольшой результат возвращается непосредственно в MCP. Большой результат
сохраняется в CSV и возвращается временной подписанной ссылкой.

### Подключения робота

Список market-data подключений задаётся сервером и колокацией робота. MCP
возвращает их как есть и не создаёт новые; пользователь может только включать
или выключать существующие подключения вне текущего read-only MCP. Некоторые
источники используются парой: например, Definitions вместе с OrderBook или
BestPrices.

Transactional connections пользователь добавляет из доступных типов. Обычно
присутствует хотя бы `0_virtual`. Перед подпиской на активные заявки проверяйте
`can_check_pos=true`, перед подпиской на позиции — `has_pos=true`: не все
подключения передают эти данные.

Для подписок на статусы, заявки и позиции сохраняйте `subscription_id`, читайте
события отдельным `get_*_updates` и обязательно вызывайте соответствующий
`unsubscribe_*`. Viking может в любой момент прислать не только update
`r="u"`, но и новый полный snapshot `r="s"`.

Для списка инструментов используется wire-type
`trans_conn.get_used_secs`. В таблице запроса официального `api.md` ошибочно
указан `trans_conn.get`; пример и ответы подтверждают
`trans_conn.get_used_secs`.

### Инструменты и клиентские коды робота

`get_robot_securities` использует `robot.get_securities`. Viking может разбить
ответ на несколько сообщений с одним `eid`; пока `data.next=true`, MCP ожидает
следующую страницу и возвращает объединённый список. `reload=false` читает
backend-кэш, `reload=true` принудительно перечитывает данные из робота.
Необязательный `sec_type` — числовая битовая маска типов инструментов.

`get_robot_client_codes` возвращает пары `sec_type` и `ll`, где `ll` — уникальная
метка клиентского кода.

`find_security` всегда требует точный `security_key` (Viking `key`). Без
`robot_id` и `portfolio` поиск выполняется по всем доступным роботам; `robot_id`
сужает его до робота, а `portfolio` — до портфеля. Результат содержит портфели,
где установлен SecKey, и найденные в формулах вхождения с исходными полями
`pos`, `text`, `sec`, `title`, `field`, `value` и `disabled`.

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
     Railway. Codex автоматически обновляет короткий access token через
     одноразовый refresh token, который тоже хранится только в RAM сервера.
     После 15 минут бездействия, завершения сессии или перезапуска Railway
     refresh становится недействительным, и Codex предлагает авторизоваться
     повторно.
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

### Шаблон портфеля

`get_portfolio_template` автоматически выполняет обязательную цепочку из
официальной документации:

1. `get_template_id` с `view="portfolio"` и идентификаторами `r_id/p_id`;
2. `get_template_by_id` с полученным `template_id`.

Инструмент возвращает полный объект `template` без фильтрации, включая
`template_fields.portfolio`, `security`, `timetable`, `notifications` и любые
дополнительные разделы. Это позволяет корректно интерпретировать динамические
поля снапшота, которые не описаны статически в WebSocket API.

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

### Логи портфеля и робота

Для новых записей логов используются два независимых lifecycle:

1. `subscribe_portfolio_logs(robot_id, portfolio)` создаёт
   `portfolio_logs.subscribe`;
2. `get_portfolio_log_updates(subscription_id, ...)` читает события `r="u"`;
3. `unsubscribe_portfolio_logs(subscription_id)` отправляет
   `portfolio_logs.unsubscribe` с `sub_eid`.

И аналогично:

1. `subscribe_robot_logs(robot_id)` создаёт `robot_logs.subscribe`;
2. `get_robot_log_updates(subscription_id, ...)` читает новые записи;
3. `unsubscribe_robot_logs(subscription_id)` отправляет
   `robot_logs.unsubscribe`.

Обе подписки возвращают первоначальный snapshot `r="s"`, `mt`, полный массив
`values` и удобные алиасы `logs`, `log_count`, `subscription_id`. Дополнительные
поля записей не фильтруются. Проверяются внешний `r_id/p_id`, идентификаторы
внутри записей, типы `level`, `msg`, `t`, `dt` и nullable `owner`.

Viking сам ограничивает видимость записей: возвращаются общие логи с пустым
автором, логи текущего email или все логи, если авторизованной роли это
разрешено. При удалении портфеля или робота Viking автоматически завершает
соответствующую подписку. После потери WebSocket-соединения нужно создать новую
подписку.

`get_robot_log_history` вызывает `robot_logs.get_history` и принимает:

- `robot_id`;
- `date_from` и `date_to` в ISO 8601 с обязательным часовым поясом;
- необязательную `message_filter` длиной до 256 символов: `*` означает любое
  число символов, `.` — один символ;
- `limit` от 1 до 100000, по умолчанию 100000.

Сервис точно преобразует даты в строки `mint`/`maxt` формата `epoch_nsec`, как
требует Viking API. Ответ сохраняет все поля логов; `dt` принимается как число
или строка цифр, поскольку таблица контракта и JSON-пример официальной
документации используют разные JSON-типы.

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
- session access token имеет тот же 15-минутный idle TTL, который сервер реально
  применяет; сессия продлевается ротируемым одноразовым refresh token только в RAM;
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


## Response contract v2

Исторические сделки, логи и текущий снапшот возвращаются без дублирования в едином массиве `items`. Сырой ответ Viking доступен только при `raw=true`. Ответ содержит `data_status`, `row_count`, `truncated`, `coverage` и `notes`; epoch-nanoseconds сохраняются, рядом добавляется `dt_iso`. Sentinel `-2^53` преобразуется в `null`. Логи по умолчанию выдаются с `verbosity=compact`; `full` нужно запрашивать явно.


## Robot portfolio trading status

`get_robot_portfolio_trading_status` получает весь список одним `robot.subscribe`. Источник статуса — только `value.re`: `n` — имя портфеля, `re=true` означает `re_sell` или `re_buy` и статус `trading`, `re=false` — `not_trading`. `portfolio.disabled` и `p_d` не используются для определения факта торговли. Никаких отдельных `portfolio.subscribe` по портфелям нет; после snapshot выполняется `robot.unsubscribe`.
