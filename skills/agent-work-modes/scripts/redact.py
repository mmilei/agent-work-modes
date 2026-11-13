"""Scrub a board snapshot before it is published, or fail if it still leaks.

    python redact.py DIR [--terms name1,name2] [--write]

Scans every .html/.json file under DIR (a `loop-status.py --snapshot` output) for what must not go public:
absolute user paths (Windows drives, /home/<user>, /Users/<user>), the private terms given with --terms (project or
people names, matched case-insensitively) and Spanish text (accented letters or common Spanish function words).
Without --write it only reports and exits 1 on any finding. With --write it replaces paths and terms in place
(`<path>`, `[redacted]`), then re-scans: Spanish is never rewritten, so it still fails and a person fixes the source.
"""
import argparse
import re
import sys
from pathlib import Path

PATH = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:(?:\\+|/+)[\w.-][^\s\"'<>]*|/(?:home|Users)/[\w.-]+[^\s\"'<>]*")
SPANISH = re.compile(r"[áéíóúñ¿¡]|\\u00(?:e1|e9|ed|f3|fa|f1|bf|a1)|\b(?:que|para|los|las|del|pero|porque|una|está|tarea|nota)\b", re.I)


word = lambda t: r"(?<!\w)" + re.escape(t) + r"(?!\w)"  # whole words: "maxi" must not hit "maximum"


def scan(text, terms):
    found = [("path", m.group()) for m in PATH.finditer(text)]
    found += [("term", m.group()) for t in terms for m in re.finditer(word(t), text, re.I)]
    found += [("spanish", m.group()) for m in SPANISH.finditer(text)]
    return found


def redact(text, terms):
    text = PATH.sub("<path>", text)
    for t in terms:
        text = re.sub(word(t), "[redacted]", text, flags=re.I)
    return text


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("dir")
    ap.add_argument("--terms", default="", help="comma-separated private words to remove")
    ap.add_argument("--write", action="store_true", help="rewrite the files in place before checking")
    args = ap.parse_args(argv)
    terms = [t.strip() for t in args.terms.split(",") if t.strip()]
    bad = 0
    for f in sorted(Path(args.dir).rglob("*")):
        if f.suffix not in (".html", ".json") or not f.is_file():
            continue
        text = f.read_text(encoding="utf-8")
        if args.write:
            text = redact(text, terms)
            f.write_text(text, encoding="utf-8")
        for kind, hit in scan(text, terms):
            bad += 1
            print(f"{f}: {kind}: {hit[:60]!r}")
    print(f"redact: {bad} finding(s)" if bad else "redact: clean")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
