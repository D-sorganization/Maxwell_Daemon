import glob

for f in glob.glob(".github/workflows/*.yml"):
    with open(f, encoding="utf-8") as file:
        content = file.read()

    new_content = content.replace(
        '          if [[ "$ONLINE" -gt 0 ]]; then',
        '          if ! [[ "$ONLINE" =~ ^[0-9]+$ ]]; then ONLINE=0; fi\n          if [[ "$ONLINE" -gt 0 ]]; then',
    )

    with open(f, "w", encoding="utf-8") as file:
        file.write(new_content)
