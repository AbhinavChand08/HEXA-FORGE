"""Developer CLIs (run from the repo root with PYTHONPATH=backend:. , see the Makefile).

  python3 -m app.cli.flatten   configs -> flat records
  python3 -m app.cli.learn     offline template mining (learn) and matching (match)
  python3 -m app.cli.mapper    registry, review, normalize and check
"""
from pathlib import Path


def iter_files(paths):
    for p in map(Path, paths):
        if p.is_dir():
            yield from sorted(f for f in p.rglob("*") if f.is_file())
        else:
            yield p


def short(v, n: int = 40) -> str:
    return v if len(v) <= n else v[: n - 3] + "..."
