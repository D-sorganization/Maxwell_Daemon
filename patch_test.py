import re

with open("tests/unit/test_daemon.py", "r") as f:
    content = f.read()

# Increase to 60s
content = content.replace("timeout=60.0", "timeout=5.0")

with open("tests/unit/test_daemon.py", "w") as f:
    f.write(content)
