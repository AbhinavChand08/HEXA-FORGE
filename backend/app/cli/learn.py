"""
Template learner CLI: offline mining (learn) and matching new configs (match).

  python3 -m app.cli.learn learn samples/configs/<vendor> ... --out registry-seed/learned/
  python3 -m app.cli.learn learn samples/configs/ --out /tmp/x --profile-from-folder
  python3 -m app.cli.learn match new_device.cfg --templates registry-seed/learned/templates.json

Writes templates.json (imported with `mapper import`) and report.txt.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

from app.cli import iter_files, short
from app.ingest.detector import detect
from app.ingest.fingerprints import load_profiles
from app.mapping.compiler import build_index
from app.mapping.matcher import match_config
from app.parsing.base import DialectHints
from app.parsing.flattener import flatten_config
from app.parsing.secrets import looks_like_hash
from app.templating.hints import LearnerHints
from app.templating.lexer import Lexer
from app.templating.miner import TemplateLearner, assign_profiles
from app.templating.signature import choose_profile


def write_report(data: dict, path: Path) -> str:
    by_profile = defaultdict(list)
    for t in data["templates"]:
        by_profile[t["vendor_family"]].append(t)

    lines = []
    summary = []
    for pname, pinfo in data["profiles"].items():
        tpls = by_profile[pname]
        n_lines = sum(t["lines"] for t in tpls)
        single = sum(1 for t in tpls if t["lines"] == 1)
        heavy = [t["id"] for t in tpls if not t["structured"] and len(t["pattern"]) >= 3
                 and sum(k != "kw" and v == "WORD" for k, v in t["pattern"])
                 / len(t["pattern"]) >= 0.5]
        enums = sum(1 for t in tpls for s in t["slots"] if s["type"] == "ENUM")
        leaked = [t["id"] for t in tpls
                  if any(k == "kw" and looks_like_hash(v) for k, v in t["pattern"])]
        head = (f"PROFILE {pname}   dialect={pinfo['dialect']}   files={len(pinfo['files'])}   "
                f"lines={n_lines}   templates={len(tpls)}   enum-slots={enums}   "
                f"lines/template={n_lines / max(len(tpls), 1):.1f}")
        summary.append(head)
        lines += ["=" * 100, head]
        lines += ["  files: " + ", ".join(pinfo["files"])]
        lines += [f"  single-line templates: {single}   "
                  f"slot-heavy templates (check these): {len(heavy)} {heavy[:10]}   "
                  f"secrets in keywords: {len(leaked)}"]
        lines += ["=" * 100]

        by_ctx = defaultdict(list)
        for t in tpls:
            by_ctx[t["context"]].append(t)
        for ctx in sorted(by_ctx, key=lambda c: (c != "", c)):
            lines.append("")
            lines.append(f"  [{ctx or 'top level'}]")
            for t in sorted(by_ctx[ctx], key=lambda t: t["display"]):
                lines.append(f"    {t['id']}  {t['display']}")
                meta = f"           {t['lines']} line(s), {t['files']} file(s)"
                vals = []
                for s in t["slots"]:
                    top = ", ".join(f"{short(v)}({c})" for v, c in list(s["values"].items())[:4])
                    vals.append(f"<{s['type']}{'...' if s['list'] else ''}>: {top}")
                if vals:
                    meta += "   " + "  |  ".join(vals)
                lines.append(meta)
                ex = t["examples"][0]
                lines.append(f"           e.g. {Path(ex['file']).name}:L{ex['line']}  "
                             f"{short(ex['raw'], 80)}")
        lines.append("")
    path.write_text("\n".join(lines))
    return "\n".join(summary)


def _load_configs(paths, fhints):
    parsed = []
    for path in iter_files(paths):
        text = path.read_text(encoding="utf-8", errors="replace")
        vendor, dialect = detect(text)
        cmds = flatten_config(text, dialect, vendor, fhints)
        parsed.append((path, dialect, cmds, vendor["name"]))
    return parsed


def cmd_learn(args):
    fhints, lhints = DialectHints.load(args.hints), LearnerHints.load(args.hints)
    parsed = _load_configs(args.paths, fhints)
    profiles = assign_profiles(parsed, args.profile_from_folder, lhints.cluster_threshold)
    profile_rows = {p["name"]: p for p in load_profiles()}

    learner = TemplateLearner(lhints)
    for pname, p in profiles.items():
        learner.learn_profile(pname, [(str(f), dialect, cmds) for f, dialect, cmds in p["members"]],
                              profile_row=profile_rows.get(pname))

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    data = learner.to_json(profiles)
    (out / "templates.json").write_text(json.dumps(data, indent=1))
    summary = write_report(data, out / "report.txt")
    print(summary)
    print(f"\nwrote {out / 'templates.json'} and {out / 'report.txt'}")


def cmd_match(args):
    data = json.loads(Path(args.templates).read_text())
    fhints = DialectHints.load(args.hints)
    lexer = Lexer(LearnerHints.load(args.hints).ifname_prefixes)
    index = build_index(data["templates"])
    sigs = {name: (p["dialect"], set(p["signature"])) for name, p in data["profiles"].items()}

    for path, dialect, cmds, vendor in _load_configs(args.paths, fhints):
        profile = choose_profile(dialect, cmds, vendor, sigs)
        print(f"\n=== {path}   dialect={dialect}   profile={profile}")
        hits = 0
        for c, best, vals, orphan in match_config(cmds, index, profile, lexer):
            review = True
            if orphan:
                result = "** UNRECOGNIZED (parent block unknown) **"
            elif best:
                hits += 1
                review = False
                result = f"{best['id']}  {best['display']}" + (f"   values={vals}" if vals else "")
                if c.negated:
                    result += "   negated"
                for v, slot in zip(vals, best["slots"]):
                    if slot["type"] == "ENUM" and v not in slot["enum"]:
                        result += f"   ** new ENUM value {v!r} -> review **"
                        review = True
            else:
                result = "** UNRECOGNIZED -> training queue **"
            if args.verbose or review:
                print(f"  L{c.src_line or '?':<4} {short(c.path_str, 70):<70} {result}")
        print(f"  coverage: {hits}/{len(cmds)} commands matched "
              f"({100 * hits / max(len(cmds), 1):.0f}%)")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Template learner")
    sub = ap.add_subparsers(dest="command", required=True)

    learn = sub.add_parser("learn", help="learn templates from a folder of configs")
    learn.add_argument("paths", nargs="+")
    learn.add_argument("--out", default="learned")
    learn.add_argument("--hints", help="hints.json (flattener + learner knobs)")
    learn.add_argument("--profile-from-folder", action="store_true",
                       help="use each file's folder name as its vendor profile")
    learn.set_defaults(func=cmd_learn)

    match = sub.add_parser("match", help="match new configs against learned templates")
    match.add_argument("paths", nargs="+")
    match.add_argument("--templates", default="registry-seed/learned/templates.json")
    match.add_argument("--hints")
    match.add_argument("-v", "--verbose", action="store_true", help="show every line")
    match.set_defaults(func=cmd_match)

    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
