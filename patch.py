import os
import glob
import re

workflows_dir = r"c:\Users\diete\Repositories\Maxwell-Daemon\.github\workflows"
for yaml_file in glob.glob(os.path.join(workflows_dir, "*.yml")):
    with open(yaml_file, "r", encoding="utf-8") as f:
        content = f.read()
    
    changed = False
    if 'mkdir -p "$(dirname "$GITHUB_OUTPUT")"' not in content:
        new_content = re.sub(
            r'([ \t]*)echo "runner=d-sorg-fleet" >> "?\$GITHUB_OUTPUT"?',
            r'\1mkdir -p "$(dirname "$GITHUB_OUTPUT")"\n\1echo "runner=d-sorg-fleet" >> "$GITHUB_OUTPUT"',
            content
        )
        if new_content != content:
            content = new_content
            changed = True
            
    if changed:
        with open(yaml_file, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"Patched {yaml_file}")
