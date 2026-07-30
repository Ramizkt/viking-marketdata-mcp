from pathlib import Path

path = Path('app/service.py')
source = path.read_text(encoding='utf-8')
source = source.replace('            "preview": rows[:preview_rows],\n', '', 1)
path.write_text(source, encoding='utf-8')
