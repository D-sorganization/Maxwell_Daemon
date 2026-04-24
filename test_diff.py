import re
from pathlib import Path
from maxwell_daemon.api import create_app
from maxwell_daemon.config import MaxwellDaemonConfig
from maxwell_daemon.daemon import Daemon

doc = Path('docs/reference/openapi.md').read_text(encoding='utf-8')
section = doc.split('## Live route inventory', maxsplit=1)[1].split('\n## ', maxsplit=1)[0]
documented_paths = set(re.findall(r'`(/[^`]+)`', section))

config = MaxwellDaemonConfig.model_validate({
    "backends": {"claude": {"type": "anthropic", "model": "test-model"}},
    "agent": {"default_backend": "claude"}
})
app = create_app(Daemon(config))
schema_paths = set(app.openapi()['paths'])

print('Missing in docs:')
for p in sorted(schema_paths - documented_paths): print(p)
print('Extra in docs:')
for p in sorted(documented_paths - schema_paths): print(p)
