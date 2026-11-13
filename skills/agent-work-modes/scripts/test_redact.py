"""Redactor checks: python skills/agent-work-modes/scripts/test_redact.py."""
import json
import tempfile
from pathlib import Path

from redact import main

with tempfile.TemporaryDirectory() as d:
    flow = Path(d, "flow.json")
    flow.write_text(json.dumps({"a": r"cd C:\Users\someone\git\secret-app && run", "b": "see /home/someone/x and /Users/bob/y",
                                "c": "SecretApp ships", "d": "end:\n```", "e": "wrote D:/work/repo/file.py"}), encoding="utf-8")
    assert main([d, "--terms", "secretapp"]) == 1, "paths and private terms are reported"
    assert flow.read_text(encoding="utf-8").count("someone") == 2, "without --write nothing changes"
    assert main([d, "--terms", "secretapp", "--write"]) == 0, "paths and terms are rewritten"
    text = flow.read_text(encoding="utf-8")
    assert "someone" not in text and "secret-app" not in text and "SecretApp" not in text and text.count("<path>") == 4
    assert "end:\\n" in text, "a drive-letter lookalike such as `end:` plus a newline is left alone"
    Path(d, "chat.json").write_text(json.dumps([{"text": "esta tarea quedó para después"}], ensure_ascii=False), encoding="utf-8")
    assert main([d, "--write"]) == 1, "Spanish is reported and never rewritten"
    assert "quedó" in Path(d, "chat.json").read_text(encoding="utf-8")
print("ok")
