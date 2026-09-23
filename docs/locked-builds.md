# Зафиксированные Python-зависимости production

## Причина

После PR #31 обнаружено расхождение: Dockerfile выполнял `pip install .`, а CI —
`uv sync --locked`. Диапазоны из pyproject.toml позволили production установить
mcp 1.30.0 / Starlette 1.7.0 вместо проверенных mcp 1.28.1 / Starlette 1.3.1.
Наблюдение было только комментарием в PR #31, не исправлением сборки.

## Новый контракт

`uv.lock` — единственный источник версий runtime Python-зависимостей. Dockerfile
копирует его вместе с pyproject.toml, README.md и исходниками app, затем выполняет:

```sh
uv sync --locked --no-dev --no-editable
uv pip check --python /app/.venv/bin/python
```

`--locked` запрещает обновлять lock при сборке: несогласованные pyproject.toml и
uv.lock завершают сборку ошибкой. Не заменять его на `--frozen`, не добавлять fallback
на pip или автоматический `uv lock`. Установка выполняется во время build, не startup.
uv 0.12.17 одинаков в Dockerfile, Dockerfile.ci и GitHub Actions.

Runtime получает только готовую `/app/.venv` (приложение установлено как wheel),
pyproject.toml и uv.lock. В runtime не копируются uv, тесты, dev-инструменты, исходная
папка app, .git, .env или пользовательские данные. Uvicorn запускается из этой venv;
`PORT`, `/health`, `/data` и существующие переменные Railway сохранены. `exec` передаёт
сигналы завершения непосредственно Uvicorn. API/MCP/OAuth-код не меняется.

Lockfile этим исправлением не обновляется. После его развёртывания production
вернётся к версиям, зафиксированным в существующем uv.lock; это не обновление библиотек.
Production не включает dev-зависимости, но все его Python-пакеты имеют те же версии,
что проверяются в CI для соответствующих платформенных/Python-маркеров.

Контракт относится к Python runtime, не к побитовому воспроизведению Docker-образа:
базовый python:3.12-slim и version-tag uv не зафиксированы digest, а изолированная
сборка wheel использует build-system из pyproject.toml. ОС и инструменты сборки —
отдельный уровень фиксации. CI проверяет фактический итоговый набор runtime-пакетов.

## Что проверяет CI

`.github/workflows/python-tests.yml` запускается на PR и push в main.

1. Python 3.11/3.12: locked install, Ruff, полные regression tests, отсутствие изменений lock.
2. Сборка настоящего production Dockerfile.
3. Полное равенство установленных пакетов и версий экспорту runtime из uv.lock,
   включая отсутствие лишних/dev-пакетов и совпадение SHA-256 lockfile.
   Маркеры requirements проверяются через packaging по окружению контейнера,
   а не по окружению CI-host. Проверяется импорт установленного wheel, не исходников.
4. Dockerfile.ci наследует собранный runtime и добавляет только dev-группу из того
   же lock. Полное сравнение inventory повторяется; app не переустанавливается.
5. В тестовом образе выполняется вся suite без внешней сети. Там нет /checks/app,
   поэтому pytest использует установленное production-приложение.
6. Отдельный неизменённый production-образ стартует штатным CMD с PORT=8765,
   временным /data и `--network none`. HTTP smoke проверяет health/setup, OAuth
   metadata, 401, CORS, DCR без scope и страницу выбора прав. Форма credentials
   не отправляется; токены не выдаются, Viking и портфели не затрагиваются.
7. Docker build с намеренно устаревшим lock в отдельном временном контексте должен
   завершиться именно ошибкой lock, а не случайным сетевым отказом.

`scripts/check_runtime_lock.py capture` использует только stdlib и создаёт inventory;
`verify` использует packaging из dev-окружения и точный экспорт `uv export --locked`.
`scripts/smoke_runtime_http.py` запускается только внутри изолированного CI-контейнера.
Его DCR-регистрация синтетическая и удаляется вместе с временным контейнером.

Локально при наличии Docker:

```sh
uv sync --locked
uv run --locked ruff check .
uv run --locked pytest -q
docker build -t viking-mcp-runtime:ci .
docker build -f Dockerfile.ci -t viking-mcp-tests:ci .
docker run --rm --network none viking-mcp-tests:ci
```

## Обновление зависимостей и rollout

Новые версии библиотек добавляются отдельным осознанным изменением uv.lock и проходят
те же проверки. Изменение только диапазона pyproject.toml без lock не должно собираться.

Перед merge проверить CI на актуальном HEAD. После разрешённого merge проверить,
что Railway развернул нужный SHA, build log содержит locked install и ожидаемые
версии, затем проверить health и доступные OAuth endpoints/авторизованный read-only
вызов. Успешный CI-контейнер не доказывает деплой или вход в реальном Claude/Lovable.

Не менять секреты, volume, OAuth registrations или `oauth-grants.sqlite3` ради этой
миграции. Существующие RAM-сессии могут потребовать повторного входа при рестарте.
Откат к старому Dockerfile снова допускает dependency drift. Откат к коду до PR #31
дополнительно игнорирует новые отзывы write; для него сначала отключить запись,
как описано в docs/oauth-compatibility.md. Не удалять хранилище отзывов.

Слияние и production-проверка фиксируются отдельно от реализации PR.

## Первичные технические источники

- https://docs.astral.sh/uv/guides/integration/docker/
- https://docs.astral.sh/uv/concepts/projects/sync/
- https://docs.astral.sh/uv/reference/cli/
