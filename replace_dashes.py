import re
from pathlib import Path

ROOT = Path(".")
EXTENSIONS = {".py", ".md"}
SKIP_DIRS = {".venv", ".git", ".pytest_cache", "__pycache__"}

count = 0
for path in ROOT.rglob("*"):
    if path.suffix not in EXTENSIONS or not path.is_file():
        continue
    if any(part in SKIP_DIRS for part in path.parts):
        continue

    text = path.read_text(encoding="utf-8")
    original = text
    text = re.sub(r"\s—\s", " - ", text)

    if text != original:
        path.write_text(text, encoding="utf-8")
        n = len(re.findall(r"\s—\s", original))
        count += n
        print(f"{path}: {n} replaced")

print(f"\nTotal replaced: {count}")