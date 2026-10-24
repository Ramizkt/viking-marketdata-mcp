# Portfolio cursor feed v1

Три дополнительных read-only MCP tools используют прежний stateless JSON `/mcp`.
Региональный узел может держать один reader напрямую к MCP и раздавать изменения
браузерам. На каждом read не нужны K1 backend, DB, claim или новая Viking подписка.
Это long-poll MCP, не server-initiated MCP/SSE transport. Старые 56 tools неизменны.

## Контракт

```text
open_portfolio_feed(portfolios, feed_id=null, lease_seconds=300)
read_portfolio_feed(feed_id, cursors, wait_seconds=25, max_events=100)
close_portfolio_feed(feed_id)
```

`portfolios`: 1..100 уникальных `{robot_id, portfolio}`. Robot ID — строка
`^[0-9]{1,20}$`, сохраняемая без преобразования через float/JS Number; совместимый
integer input допустим только 0..9007199254740991. Имя — точная строка 1..200 символов.
Renew с существующим feed_id допускает только тот же exact set и OAuth principal
(client_id + fingerprint credentials); обновление access token этого principal
не требует нового feed_id. Идентификатор ленты не заменяет OAuth.

Open/renew возвращает `structuredContent`:

```json
{
  "protocol_version": 1,
  "feed_id": "opaque-id",
  "lease_expires_at": 1780000300,
  "items": [{
    "type": "snapshot", "stream_id": "opaque-stream", "robot_id": "305",
    "portfolio": "example", "epoch": "opaque-epoch", "seq": 0,
    "source_ts_ns": "1780000000000000001", "source_ts_iso": "2026-05-28T20:26:40.000000001Z",
    "value": {"name": "example", "securities": {}}, "deleted": false,
    "reset_reason": "initial"
  }],
  "more_available": false, "data_status": "ok", "notes": [],
  "streams": [{"stream_id": "opaque-stream", "state": "live"}]
}
```

В read передаётся один `{stream_id, epoch, seq}` для каждого stream ленты, без
пропусков/дубликатов. `wait_seconds` 0..30: возвращает уже доступное сразу либо
ждёт изменение **любого** портфеля. `max_events` 1..500. Результат имеет ту же
оболочку; пустой wait явно содержит `data_status=empty` и причину в `notes`.
`streams.state`: `live`, `reconnecting`, `deleted`. Это диагностика текущего
upstream состояния, не доказательство актуальности каждой отдельной биржевой величины.
`close` возвращает `{feed_id, closed:true}`.

## Snapshot, patch, replay

- Начальный snapshot имеет seq=0. Каждый следующий upstream event повышает seq
  соответствующего stream на 1. Между разными streams общего порядка нет.
- `patch.value` — рекурсивный partial update. Объекты сливаются по ключам; вложенный
  `{__action:"del"}` удаляет ключ; массивы, scalar и null заменяют значение. Например,
  `uf1:{v:2}` сохраняет предыдущий `uf1.c`, а securities обновляются по sec_key.
  Неизвестные поля сохраняются. Sentinel -9007199254740992 нормализуется в null.
- `snapshot.value` полностью заменяет состояние, включая повторный upstream r=s.
  `deleted:true` сообщает удаление портфеля и завершение его upstream подписки.
- После применения frame сохранить его epoch/seq. Продвигать только cursors
  фактически полученных frames. При more_available читать снова без ожидания.
- Повтор того же cursor не удаляет данные: каждый downstream читает независимо.
  Replay ограничен на stream 1000 events, 2 MB JSON и 120 секундами; snapshot хранится
  отдельно. Переполнение/устаревший или будущий cursor даёт snapshot текущего seq
  с `reset_reason=cursor_gap`, а не молчаливую потерю обновлений.
- Reconnect/overflow upstream создаёт новую подписку, epoch и полный snapshot.
  Ответ старому cursor: `reset_reason=epoch_changed`. Возможны также `initial`,
  `renew`, `upstream_snapshot`. Нельзя продолжать применять patches прежней epoch.
- `source_ts_ns` — исходный Viking `ts` в epoch nanoseconds, строка без потери точности;
  это время ответа Viking, не локального decode и не обязательно время биржевого изменения.
  Private upstream eid и дублирующий raw data не возвращаются намеренно: legacy
  get_portfolio_updates не должен конкурировать с единственным внутренним drainer.

## Lifecycle и ошибки

Один upstream consumer используется всеми лентами с теми же credentials + robot +
portfolio. Каждая lease принадлежит OAuth client + credentials; нельзя читать или
закрывать чужой feed. Последняя lease вызывает unsubscribe. Смена credentials
создаёт отдельный hub. Legacy subscribe/get/unsubscribe остаются независимыми.
Максимум 32 активных feeds на principal, до 8 параллельных initial subscribe на open.

Lease 1..300 секунд, ограничена access-token expiry. Read не продлевает lease;
renew выполняется через open с тем же feed_id. Cleanup проверяется каждую секунду;
ожидающий read также прерывается по expiry. Shutdown освобождает подписки.
Хранилище RAM рассчитано на одну реплику: restart теряет feed_id/replay; клиент
открывает новый feed со snapshot. Несколько реплик требуют affinity либо общего
хранилища/координатора. Никакого durable replay эта версия не обещает.

Ошибки возвращаются с `isError=true` и `structuredContent.feed_error`:

| Код | Действие клиента |
|---|---|
| `feed_expired`, `feed_not_found` | Fresh open без старого id, если node grant ещё действителен |
| `scope_mismatch`, `cursor_invalid`, `invalid_request` | Исправить запрос; не расширять разрешённый scope |
| `capacity_exceeded` | Закрыть ненужные ленты |
| `upstream_unavailable` | Ограниченный retry с backoff |
| `upstream_rejected` | Исходный Viking API error с code/api_response, без автоматического обхода |
| `authorization_required` | Прекратить выдачу данных, повторить OAuth |
| `upstream_failed` | Явная ошибка; диагностировать, не подменять данные кешем |

Schema validation errors FastMCP возникают до tool и используют стандартный MCP
error path. Read permission проверяется при входе в каждый tool и после ожидания,
до выдачи данных. Истёкший/удалённый session token не получает накопленные события.
Общие OAuth правила не изменены: нынешний remembered self-contained token не имеет
новой глобальной denylist; write grant generation не отзывает read. При отзыве
доступа текущий background hub может существовать до последней bounded lease,
но повторные авторизованные tools проверяют provider заново. Политику K1 route,
connection revision, disabled и scoped node grant проверяют K1/node при admission
и renew; этот MCP не обращается к K1 DB. Для немедленного внешнего отзыва нужен
отдельный invalidation канал; без него предел задержки — expiry выданного grant.

Проверки используют синтетические fixtures; реальная скорость, production rollout
и поведение конкретного узла требуют отдельной приёмки после разрешённого deploy.
