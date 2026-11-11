"""Flags that trim what a headless `claude -p` session loads before reading its prompt (see fresh-loop.py --lean)."""
import json
import subprocess
from pathlib import Path


def lean_flags(tools, cwd="."):
    """Only `tools`, no skills, no MCP servers, no CLAUDE.md from folders above the repo root (above cwd outside a repo)."""
    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, cwd=cwd).stdout.strip()
    except FileNotFoundError:
        top = ""
    above = [(p / name).as_posix() for p in Path(top or cwd).resolve().parents
             for name in ("CLAUDE.md", "CLAUDE.local.md") if (p / name).is_file()]
    flags = ["--tools", tools, "--disable-slash-commands", "--strict-mcp-config"]
    return flags + (["--settings", json.dumps({"claudeMdExcludes": above})] if above else [])
