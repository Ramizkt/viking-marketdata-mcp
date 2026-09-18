"""Temporary reviewed refinements; removed with the integration workflow."""
import runpy
from pathlib import Path

runpy.run_path("scripts/integrate_portfolio_controls.py", run_name="__main__")

p = Path("app/oauth.py")
s = p.read_text()
s = s.replace('''        if PORTFOLIO_WRITE_SCOPE in (pending.params.scopes or []):
            if not self.settings.viking_portfolio_writes_enabled or form.get("allow_portfolio_writes") != "yes":
                return self._render_page(
                    pending_id, selected_mode=mode, email=email, role=role,
                    error="Для изменения полей и остановок нужно отдельное явное разрешение на операции записи.",
                )''', '''        if PORTFOLIO_WRITE_SCOPE in (pending.params.scopes or []) and (
            not self.settings.viking_portfolio_writes_enabled
            or form.get("allow_portfolio_writes") != "yes"
        ):
            return self._render_page(
                pending_id, selected_mode=mode, email=email, role=role,
                error="Нужно отдельное явное разрешение на изменение полей и остановки портфелей.",
            )''')
p.write_text(s)

p = Path("app/portfolio_control.py")
s = p.read_text()
s = s.replace("import asyncio\n", "import asyncio\nimport contextlib\n")
s = s.replace('''            await client.close()
            raise''', '''            with contextlib.suppress(Exception):
                await client.close()
            raise''')
replacements = {
    'Read back the affected fields with get_current_portfolio_data; a write acknowledgement is not readback.':
        'Read back fields with get_current_portfolio_data; acknowledgement is not readback.',
    'Formulas remain active and may re-enable trading. Use Stop formulas only on explicit user instruction.':
        'Formulas can re-enable trading. Stop formulas requires a separate explicit user instruction.',
    'Stop formulas also disables formula calculations and changes formula-driven modes to constants/Standard.':
        'Stop formulas disables formulas and changes formula-driven modes to constants/Standard.',
    'Accepted means Viking acknowledged the command, not verified trading state or exchange cancellation.':
        'Accepted means API acknowledgement, not verified trading state or exchange cancellation.',
    'Use get_robot_portfolio_trading_status for trading state and order subscriptions for active orders.':
        'Verify trading via get_robot_portfolio_trading_status and active orders via subscriptions.',
    'Preview is not a reservation: permissions, templates and portfolio state can change before execution.':
        'Preview is not a reservation: permissions, templates and state may change before execution.',
    'Stop operations do not close positions. No automatic rollback or replay is performed.':
        'Stops do not send position-closing commands. No automatic rollback or replay is performed.',
}
for old, new in replacements.items():
    s = s.replace(old, new)
p.write_text(s)

p = Path("app/portfolio_tools.py")
s = p.read_text().replace(
    'and explicitly consent in the browser. Existing grants are not upgraded automatically.',
    'and consent in the browser. Old grants are not upgraded automatically.'
)
p.write_text(s)

p = Path("README.md")
s = p.read_text().replace("вне текущего read-only MCP", "вне текущего MCP")
s = s.replace("- сервер предоставляет только read-only инструменты.",
              "- чтение доступно по viking.read; пять операций записи требуют отдельного разрешения.")
s = s.replace("https://fkviking.github.io/bot-doc/docs/interface.html", "https://fkviking.github.io/bot-doc/docs/getting-started.html")
p.write_text(s)

p = Path("AGENTS.md")
s = p.read_text().replace("аргументы, read-only annotations, понятное описание", "аргументы, корректные annotations, понятное описание")
s = s.replace("- внешний MCP tool schema и read-only annotation.",
              "- внешний MCP tool schema и корректные read/write annotations.")
p.write_text(s)
print("Refinements applied; no live portfolio calls were made.")
