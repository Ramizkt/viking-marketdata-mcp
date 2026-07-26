from __future__ import annotations

from html import escape
from pathlib import Path

from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, PlainTextResponse

CLIENT_DIR = Path(__file__).resolve().parent.parent / "client" / "windows"


async def setup_page(request: Request) -> HTMLResponse:
    base_url = str(request.base_url).rstrip("/")
    session_script = f"{base_url}/client/windows/viking-session.ps1"
    save_script = f"{base_url}/client/windows/save-viking-credentials.ps1"
    file_script = f"{base_url}/client/windows/viking-file.ps1"
    html = f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Подключение Viking Market Data MCP</title>
  <style>
    body {{ font: 16px/1.5 system-ui, sans-serif; max-width: 880px; margin: 40px auto;
            padding: 0 20px; color: #172033; }}
    .card {{ border: 1px solid #d8deea; border-radius: 14px; padding: 22px; margin: 18px 0; }}
    code {{ background: #f3f5f9; padding: 2px 5px; border-radius: 5px; }}
    pre {{ background: #111827; color: #f9fafb; padding: 16px; border-radius: 10px; overflow: auto; }}
    a {{ color: #2457d6; }}
  </style>
</head>
<body>
  <h1>Подключение Viking Market Data MCP</h1>
  <p>Выберите, где будут находиться ваши Viking credentials. Не отправляйте API key в чат.</p>
  <section class="card">
    <h2>1. Только текущая сессия</h2>
    <p>Credentials нигде не записываются. Они находятся только в памяти PowerShell,
       Codex и Railway. На Railway они удаляются после 15 минут без запросов или при перезапуске сервера.</p>
    <ol>
      <li>Полностью закройте Codex App.</li>
      <li><a href="{escape(session_script)}">Скачайте viking-session.ps1</a>.</li>
      <li>Запустите в PowerShell:
        <pre>powershell -ExecutionPolicy Bypass -File .\\viking-session.ps1</pre>
      </li>
    </ol>
  </section>
  <section class="card">
    <h2>2. Зашифрованный локальный файл</h2>
    <p>Файл хранится только на вашем компьютере. API key защищён Windows DPAPI и
       расшифровывается только под вашей учётной записью. Файл читает launcher, а не модель.</p>
    <ol>
      <li><a href="{escape(save_script)}">Скачайте save-viking-credentials.ps1</a>,
          <a href="{escape(file_script)}">viking-file.ps1</a> и
          <a href="{escape(session_script)}">viking-session.ps1</a> в одну папку.</li>
      <li>Один раз создайте файл:
        <pre>powershell -ExecutionPolicy Bypass -File .\\save-viking-credentials.ps1</pre>
      </li>
      <li>Для запуска Codex:
        <pre>powershell -ExecutionPolicy Bypass -File .\\viking-file.ps1</pre>
      </li>
    </ol>
  </section>
  <p>В обоих режимах Railway не записывает email, API key или role в переменные,
     базу данных или файлы. Закройте Codex App после работы; серверная копия в RAM
     будет автоматически удалена после периода бездействия.</p>
</body>
</html>"""
    return HTMLResponse(html)


async def client_script(request: Request):
    filename = request.path_params["filename"]
    allowed = {
        "viking-session.ps1",
        "save-viking-credentials.ps1",
        "viking-file.ps1",
    }
    if filename not in allowed:
        return PlainTextResponse("Not found", status_code=404)
    return FileResponse(
        CLIENT_DIR / filename,
        media_type="text/plain; charset=utf-8",
        filename=filename,
    )
