"""Read-only evidence: run from any directory while the local board serves port 8774."""
import hashlib
import importlib.util
import json
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "skills/agent-work-modes/scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("board_evidence", SCRIPTS / "loop-status.py")
board = importlib.util.module_from_spec(spec)
spec.loader.exec_module(board)


def get(path):
    with urllib.request.urlopen("http://127.0.0.1:8774" + path, timeout=60) as response:
        assert response.status == 200
        return response.read().decode("utf-8")


page = get("/")
assert "function usageAmount" in page and "additional attempt" in page
flow = json.loads(get("/flow.json"))
assert all("verify_failures" in item for item in flow["items"]), "server still runs the old Python code"
camera = next(item for item in flow["items"] if item["id"] == "2")
r1 = next(item for item in flow["items"] if item["id"] == "1")
assert (camera["tries"], camera["verify_failures"]) == ("3", 0)
assert (r1["tries"], r1["verify_failures"]) == ("2", 0)
graph = json.loads(get("/graph.json?hours=168"))
root_id = "x:" + flow["execution"]["session"]
root = next(node for node in graph["nodes"] if node["id"] == root_id)
assert root["usage_available"] and len(root["source_tokens"]) > 1
parts = [board.codex_file(Path(source)) for source in root["source_tokens"]]
assert all(part["usage_independent"] and part["usage_available"] for part in parts)
events = {key: value for part in parts for key, value in part["usage_events"].items()}
expected = [sum(value[i] for value in events.values()) for i in range(4)]
assert root["tok"] == expected, "served counters differ from verified local usage events"
assert root["calls"] == len(events)
record = {
    "checked_at": datetime.now().astimezone().isoformat(),
    "port": 8774,
    "http_routes_ok": ["/", "/flow.json", "/graph.json?hours=168"],
    "camera": {"attempts": int(camera["tries"]), "explicit_verify_failures": camera["verify_failures"]},
    "r1": {"attempts": int(r1["tries"]), "explicit_verify_failures": r1["verify_failures"]},
    "coordinator": {"rollouts": len(parts), "independent_local_counters": True,
                    "tokens": root["tokens"], "unique_usage_events": len(events),
                    "served_counters_match_sources": True},
    "sha256": {name: hashlib.sha256((SCRIPTS / name).read_bytes()).hexdigest() for name in
               ("loop-status.py", "loop-status.html", "test_loop_status.py", "test_usage.js")},
    "privacy": "No transcripts, session IDs, prompts or source paths are exported.",
}
print(json.dumps(record, indent=2))
