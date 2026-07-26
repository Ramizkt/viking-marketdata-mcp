from __future__ import annotations

from starlette.requests import Request
from starlette.responses import HTMLResponse


async def setup_page(_: Request) -> HTMLResponse:
    return HTMLResponse(
        """<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Viking Market Data MCP</title>
  <style>
    :root { color-scheme: dark; font-family: Inter, system-ui, sans-serif; }
    body { margin: 0; background: #0d0f12; color: #f7f8fa; }
    main { max-width: 720px; margin: 0 auto; padding: 64px 24px; }
    h1 { font-size: clamp(32px, 6vw, 52px); margin-bottom: 14px; }
    p { color: #b9c0cc; font-size: 18px; line-height: 1.55; }
    .box { margin-top: 28px; border: 1px solid #343b47; border-radius: 16px;
           background: #15181e; padding: 24px; }
    strong { color: white; }
  </style>
</head>
<body>
<main>
  <h1>Viking Market Data MCP</h1>
  <p>Добавьте MCP-адрес в Codex или другой совместимый клиент. Затем нажмите
     <strong>«Авторизоваться»</strong>.</p>
  <div class="box">
    <p>В браузере появятся два варианта:</p>
    <p><strong>Только на эту сессию</strong><br>
       Credentials находятся только в оперативной памяти.</p>
    <p><strong>Запомнить на этом компьютере</strong><br>
       Codex сохраняет зашифрованный OAuth-токен локально.</p>
  </div>
</main>
</body>
</html>""",
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": (
                "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'"
            ),
            "X-Content-Type-Options": "nosniff",
        },
    )
