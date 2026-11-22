"""Read only a selected, single-line credential from the private dotenv file."""

from io import StringIO
from pathlib import Path
import re

from dotenv import dotenv_values


def read_secret(name: str, path: str = ".env") -> str:
    file = Path(path)
    if not file.is_file():
        return ""
    pattern = re.compile(rf"^\s*(?:export\s+)?{re.escape(name)}\s*=")
    selected = ""
    with file.open(encoding="utf-8") as stream:
        for line in stream:
            if pattern.match(line):
                selected = dotenv_values(stream=StringIO(line), interpolate=False).get(name) or ""
    return selected.strip()
