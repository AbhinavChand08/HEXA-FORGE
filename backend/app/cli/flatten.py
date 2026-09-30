"""
Vendor-agnostic config flattener CLI.

  python3 -m app.cli.flatten samples/configs/                 # files or folders
  python3 -m app.cli.flatten r1.cfg --output jsonl            # structured records
  python3 -m app.cli.flatten r1.cfg --profile cisco           # force a vendor profile
  python3 -m app.cli.flatten r1.cfg --hints hints.json        # override knobs
  python3 -m app.cli.flatten r1.cfg --profiles profiles.json  # add vendor rows
"""
from __future__ import annotations

import argparse
import json

from app.cli import iter_files
from app.ingest.detector import DIALECTS, detect
from app.ingest.fingerprints import load_profiles
from app.parsing.base import DialectHints
from app.parsing.flattener import flatten_config


def main(argv=None):
    ap = argparse.ArgumentParser(description="Vendor-agnostic config flattener")
    ap.add_argument("paths", nargs="+", help="config files or folders")
    ap.add_argument("--hints", help="JSON file overriding DialectHints knobs")
    ap.add_argument("--profile", help="force a vendor profile (default: detected)")
    ap.add_argument("--profiles", help="JSON / YAML list of extra / replacement profile rows")
    ap.add_argument("--dialect", choices=DIALECTS, help="force a dialect")
    ap.add_argument("--device-id", help="default: hostname in the config, else file name")
    ap.add_argument("--output", choices=["text", "jsonl"], default="text")
    ap.add_argument("--leaves-only", action="store_true", help="omit block headers")
    args = ap.parse_args(argv)

    hints = DialectHints.load(args.hints)
    profiles = load_profiles(args.profiles)
    for path in iter_files(args.paths):
        text = path.read_text(encoding="utf-8", errors="replace")
        profile, dialect = detect(text, profiles, args.profile, args.dialect)
        commands = flatten_config(text, dialect, profile, hints, args.device_id)
        for c in commands:
            c.device_id = c.device_id or path.stem
        if args.leaves_only:
            commands = [c for c in commands if not c.is_block]
        if args.output == "jsonl":
            for c in commands:
                print(json.dumps({"file": str(path), "dialect": dialect,
                                  "profile": profile["name"], **c.to_dict()}))
            continue
        depth = max((c.depth for c in commands), default=0)
        device = commands[0].device_id if commands else path.stem
        print(f"\n=== {path}   dialect={dialect}   profile={profile['name']}   device={device}"
              f"   commands={len(commands)}   negated={sum(c.negated for c in commands)}"
              f"   max_depth={depth}      (B = block, N = negated)")
        for c in commands:
            ln = f"L{c.src_line}" if c.src_line else "L?"
            flags = ("B" if c.is_block else " ") + ("N" if c.negated else " ")
            print(f"  {ln:>5} [{flags}] {c.path_str}")


if __name__ == "__main__":
    main()
