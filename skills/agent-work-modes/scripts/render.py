"""Render the catalog (methods.json) into one self-contained HTML page.

    python render.py                                  # full page -> docs/index.html at the repo root
    python render.py --pick goal,stophook --out x.html  # same page, those forms highlighted as candidates
    python render.py --pick goal --notes notes.json     # plus a per-task block on top:
                                                        # {"goal": {"looks": "...", "for": "...", "against": "...", "cost": "..."}}
"""
import argparse
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
CATALOG = HERE.parent / "methods.json"

ap = argparse.ArgumentParser()
ap.add_argument("--pick", default="", help="comma-separated form ids to highlight")
ap.add_argument("--notes", help="JSON file: form id -> looks/for/against/cost for this task")
ap.add_argument("--out", default=str(HERE.parents[2] / "docs" / "index.html"))
args = ap.parse_args()

data = json.loads(CATALOG.read_text(encoding="utf-8"))
ids = {f["id"] for f in data["forms"]}
pick = [p for p in args.pick.split(",") if p]
unknown = [p for p in pick if p not in ids]
if unknown:
    raise SystemExit(f"unknown form ids: {', '.join(unknown)}; valid: {', '.join(sorted(ids))}")

notes = json.loads(Path(args.notes).read_text(encoding="utf-8")) if args.notes else None
if notes:
    bad = [k for k in notes if k not in ids]
    if bad:
        raise SystemExit(f"unknown form ids in notes: {', '.join(bad)}")

# "</" inside the JSON would close the <script> tag early
inline = lambda v: json.dumps(v, ensure_ascii=False).replace("</", "<\\/")
page = (HERE / "page.html").read_text(encoding="utf-8")
page = page.replace("/*DATA*/null", inline(data)).replace("/*PICK*/[]", inline(pick)).replace("/*NOTES*/null", inline(notes))
out = Path(args.out)
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(page, encoding="utf-8")
print(out)
