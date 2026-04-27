import re
from pathlib import Path

from maxwell_daemon.api import create_app
from maxwell_daemon.config import MaxwellDaemonConfig


class _OpenAPIDocsDaemon:
    def __init__(self, config):
        self._config = config


doc = Path("docs/reference/openapi.md").read_text(encoding="utf-8")
section = doc.split("## Live route inventory", maxsplit=1)[1].split("\n## ", maxsplit=1)[0]
documented_paths = set(re.findall(r"`(/[^`]+)`", section))

app = create_app(_OpenAPIDocsDaemon(MaxwellDaemonConfig()))
schema_paths = set(app.openapi()["paths"])

print("--- Documented but not in schema ---")
for p in documented_paths - schema_paths:
    print(p)

print("--- In schema but not documented ---")
for p in schema_paths - documented_paths:
    print(p)
