import re
from pathlib import Path

from maxwell_daemon.api import create_app
from maxwell_daemon.config import MaxwellDaemonConfig
from maxwell_daemon.config.models import BackendConfig


class _OpenAPIDocsDaemon:
    def __init__(self, config):
        self._config = config


doc = Path("docs/reference/openapi.md").read_text(encoding="utf-8")
section = doc.split("## Live route inventory", maxsplit=1)[1].split("\n## ", maxsplit=1)[0]
documented_paths = set(re.findall(r"`(/[^`]+)`", section))

config = MaxwellDaemonConfig(backends={"test": BackendConfig(name="test")})
app = create_app(_OpenAPIDocsDaemon(config))
schema_paths = set(app.openapi()["paths"])

print("Documented but not in schema:", documented_paths - schema_paths)
print("In schema but not documented:", schema_paths - documented_paths)
