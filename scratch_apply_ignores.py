import re
import sys


def process_mypy_output(filepath):
    pattern = re.compile(r"^([^:]+):(\d+): (?:error|note): .*?\[([^\]]+)\]$")

    fixes = {}
    removes = {}

    with open(filepath, encoding="utf-16") as f:
        for line in f:
            match = pattern.match(line.strip())
            if match:
                file_path = match.group(1)
                line_num = int(match.group(2))
                error_code = match.group(3)

                if error_code == "unused-ignore":
                    if file_path not in removes:
                        removes[file_path] = set()
                    removes[file_path].add(line_num)
                else:
                    if file_path not in fixes:
                        fixes[file_path] = {}
                    if line_num not in fixes[file_path]:
                        fixes[file_path][line_num] = set()
                    fixes[file_path][line_num].add(error_code)

    all_files = set(fixes.keys()).union(set(removes.keys()))
    for file_path in all_files:
        try:
            with open(file_path, encoding="utf-8") as f:
                lines = f.readlines()

            # Handle unused-ignore
            if file_path in removes:
                for line_num in removes[file_path]:
                    idx = line_num - 1
                    if idx < len(lines):
                        original = lines[idx]
                        lines[idx] = re.sub(r"\s*# type: ignore.*", "", original)
                        if not lines[idx].endswith("\n"):
                            lines[idx] += "\n"

            # Handle new errors
            if file_path in fixes:
                for line_num, codes in fixes[file_path].items():
                    idx = line_num - 1
                    if idx >= len(lines):
                        continue

                    original = lines[idx].rstrip("\n")

                    # Check if it already has a type ignore
                    if "# type: ignore" in original:
                        # Append new codes to existing ones
                        match = re.search(r"# type: ignore\[(.*?)\]", original)
                        if match:
                            existing_codes = set(match.group(1).split(","))
                            all_codes = existing_codes.union(codes)
                            new_str = ",".join(sorted(all_codes))
                            lines[idx] = (
                                re.sub(
                                    r"# type: ignore\[.*?\]",
                                    f"# type: ignore[{new_str}]",
                                    original,
                                )
                                + "\n"
                            )
                        else:
                            # It has a bare type: ignore, which should ignore everything anyway, but mypy reported an error?
                            # Maybe we just replace it.
                            codes_str = ",".join(sorted(codes))
                            lines[idx] = (
                                re.sub(
                                    r"# type: ignore.*",
                                    f"# type: ignore[{codes_str}]",
                                    original,
                                )
                                + "\n"
                            )
                    else:
                        # Append type ignore
                        codes_str = ",".join(sorted(codes))
                        lines[idx] = f"{original}  # type: ignore[{codes_str}]\n"

            with open(file_path, "w", encoding="utf-8") as f:
                f.writelines(lines)
            print(f"Patched {file_path}")
        except Exception as e:
            print(f"Error processing {file_path}: {e}")


if __name__ == "__main__":
    process_mypy_output(sys.argv[1])
