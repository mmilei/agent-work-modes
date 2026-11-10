#!/usr/bin/env python3
"""Run the selector evals: python evals/run.py [--limit N] [--model M] [--triggers] [--out FILE]

Scenarios: one `claude -p` call each with the plugin loaded; the model returns candidate ids and its
recommendation as JSON. Pass = every `expected` id is a candidate and the recommendation is not `forbidden`.
Triggers (--triggers): pass = the Skill tool fired (trigger list) or did not (no_trigger list).
Writes a markdown table to --out; a full run (no --limit) also refreshes the table in README.md between the
evals markers. Stops if the plan's usage limit is hit. Plugin eval (`claude plugin eval`) was not used: it needs case.yaml and publishes to claude.ai.
"""
import argparse, json, re, subprocess, sys, datetime, pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCHEMA = {"type": "object", "required": ["candidates", "recommended"], "properties": {
    "candidates": {"type": "array", "items": {"type": "string"}}, "recommended": {"type": "string"}}}
PROMPT = ("{request}\n\nContext (these are the user's answers to any clarifying question; do not ask more, "
          "do not render or open the page, do not set anything up): {context}\n\nUse the agent-work-modes skill. "
          "Return JSON: candidates = form ids from methods.json you would present, recommended = the one you recommend.")
MODELS = set()
START, END = "<!-- evals:start -->", "<!-- evals:end -->"


def claude(prompt, model, *extra):
    cmd = ["claude", "-p", prompt, "--plugin-dir", str(ROOT), "--disallowedTools", "Bash,Write,Edit,AskUserQuestion", *extra]
    if model:
        cmd += ["--model", model]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", cwd=ROOT, timeout=600)
    try:  # the last line is the result event in both json and stream-json
        res = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        res = {}
    if res.get("is_error") and re.search(r"limit", str(res.get("result")) + r.stderr, re.I):
        sys.exit(f"stopped: plan usage limit reached: {res.get('result')}")
    MODELS.update(re.findall(r'"model":\s*"(claude-[^"]+)"', r.stdout))
    return r.stdout


def scenario(s, model):
    out = claude(PROMPT.format(**s), model, "--output-format", "json", "--json-schema", json.dumps(SCHEMA))
    try:
        d = json.loads(out)
        MODELS.update(d.get("modelUsage", {}))
        res = d.get("structured_output") or json.loads(d["result"])
        cands, rec = res["candidates"], res["recommended"]
    except Exception as e:
        return False, f"unparsable ({e.__class__.__name__}): {out[:80]!r}"
    ok = set(s["expected"]) <= set(cands) and rec not in s["forbidden"]
    return ok, f"candidates={cands} recommended={rec}"


def trigger(phrase, should, model):
    out = claude(phrase, model, "--output-format", "stream-json", "--verbose", "--max-turns", "3")
    fired = '"name":"Skill"' in out.replace(" ", "")
    return fired == should, f"skill fired={fired}"


def main():
    a = argparse.ArgumentParser()
    a.add_argument("--limit", type=int)
    a.add_argument("--model")
    a.add_argument("--triggers", action="store_true")
    a.add_argument("--out", default=str(ROOT / "evals" / "results.md"))
    a = a.parse_args()
    rows = []
    for s in json.load(open(ROOT / "evals" / "scenarios.json", encoding="utf-8"))["scenarios"][: a.limit]:
        rows.append(("scenario", s["id"], *scenario(s, a.model)))
    if a.triggers:
        t = json.load(open(ROOT / "evals" / "triggers.json", encoding="utf-8"))
        for key, should in (("trigger", True), ("no_trigger", False)):
            for p in t[key][: a.limit]:
                rows.append((key, p, *trigger(p, should, a.model)))
    lines = [f"Run {datetime.date.today()}, model {', '.join(sorted(MODELS)) or a.model or 'default'}", "", "| kind | case | result | detail |", "|---|---|---|---|"]
    lines += [f"| {k} | {c} | {'pass' if ok else 'FAIL'} | {d} |" for k, c, ok, d in rows]
    lines.append(f"\n{sum(r[2] for r in rows)}/{len(rows)} passed")
    pathlib.Path(a.out).write_text("\n".join(lines) + "\n", encoding="utf-8")
    readme = ROOT / "README.md"
    if a.limit is None and START in (text := readme.read_text(encoding="utf-8")):
        head, rest = text.split(START, 1)
        readme.write_text(f"{head}{START}\n" + "\n".join(lines) + f"\n{END}{rest.split(END, 1)[1]}", encoding="utf-8")
    print("\n".join(lines))
    sys.exit(0 if all(r[2] for r in rows) else 1)


if __name__ == "__main__":
    main()
