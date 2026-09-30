"""Vendor profiles and fingerprint-based vendor identification (design 5.4, 6.2, 9.1).

A profile row is data. registry-seed/vendors/<vendor>/profile.yaml seeds the
vendor_profile table (the folder name is THE vendor id: templates, mappings and vendor
defaults use it); at runtime the rows come from the registry.
  name            vendor id
  family          vendor family (default: name)
  parent          vendor id this profile inherits templates, mappings and defaults from
  dialect         parser to use; null = detected per file
  dialect_params  parser knobs, design names or DialectHints names (parsing.base
                  .hints_from_params); by_dialect: {dialect: knobs} for rows whose
                  dialect is detected per file
  fingerprints    [{pattern, weight}]; pattern is a regex searched in multiline mode
                  (so '^' anchors at a line start)
  threshold       minimum summed weight to claim a config
  status          onboarding | ready
Scoring runs over detection_window(text): the first WINDOW_LINES lines plus any XML
namespace declarations (design 5.4), not the whole file, so a marker deep inside a huge
config cannot outweigh the vendor's usual header fingerprints.
The highest score at or above its threshold wins (ties: first row in vendor-name order).
Declared-vendor handling, near-ties (NEEDS_VENDOR) and the signature-overlap signal are in
app.ingest.detector.detect_vendor, which is what task D2 (ambiguity) and job ingestion use.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

import yaml

from app import settings
from app.parsing.base import hints_from_params
from app.parsing.dialects import PARSERS

PROFILE_DEFAULTS = {"family": None, "parent": None, "dialect": None, "dialect_params": {},
                    "fingerprints": [], "threshold": 1, "status": "onboarding"}
PROFILE_FIELDS = ("name", *PROFILE_DEFAULTS)
STATUSES = ("onboarding", "ready")
GENERIC = {**PROFILE_DEFAULTS, "name": "generic", "family": "generic"}

WINDOW_LINES = 200
_XMLNS = re.compile(r'xmlns(:[\w.-]+)?\s*=\s*["\']([^"\']+)["\']')


def profile_row(name: str, data: dict) -> dict:
    """A profile row with every field present (family defaults to the name)."""
    unknown = set(data) - set(PROFILE_FIELDS)
    if unknown:
        raise ValueError(f"profile {name}: unknown field(s) {sorted(unknown)}")
    row = {**PROFILE_DEFAULTS, **data, "name": name}
    row["family"] = row["family"] or name
    row["dialect_params"] = row["dialect_params"] or {}
    row["fingerprints"] = row["fingerprints"] or []
    return row


def validate_profile(row: dict) -> list[str]:
    """Checks for a profile row written by the seed or the UI. -> errors"""
    errors = []
    if row["dialect"] is not None and row["dialect"] not in PARSERS:
        errors.append(f"unknown dialect {row['dialect']!r}")
    if row["status"] not in STATUSES:
        errors.append(f"status must be one of {STATUSES}")
    params = row["dialect_params"]
    for label, p in [("dialect_params", params)] + [
            (f"by_dialect.{d}", p) for d, p in (params.get("by_dialect") or {}).items()]:
        try:
            hints_from_params(p)
        except ValueError as e:
            errors.append(f"{label}: {e}")
    for d in params.get("by_dialect") or {}:
        if d not in PARSERS:
            errors.append(f"by_dialect: unknown dialect {d!r}")
    for fp in row["fingerprints"]:
        try:
            _regex(fp["pattern"])
            float(fp.get("weight", 1))
        except (KeyError, TypeError, ValueError, re.error) as e:
            errors.append(f"fingerprint {fp!r}: {e}")
    return errors


@lru_cache(maxsize=None)
def _seed_profiles(seed_dir: str) -> tuple[dict, ...]:
    return tuple(profile_row(path.parent.name, yaml.safe_load(path.read_text()) or {})
                 for path in sorted(Path(seed_dir).glob("vendors/*/profile.yaml")))


def load_profiles(extra: str | None = None, seed_dir: Path | None = None) -> list[dict]:
    """Seed profile rows (for offline tools; the pipeline reads the registry), with rows
    from a JSON / YAML list file added / replacing by name."""
    rows = [dict(r) for r in _seed_profiles(str(seed_dir or settings.SEED_DIR))]
    if not extra:
        return rows
    text = Path(extra).read_text()
    added = [profile_row(r["name"], {k: v for k, v in r.items() if k != "name"})
             for r in (json.loads(text) if extra.endswith(".json") else yaml.safe_load(text))]
    names = {row["name"] for row in added}
    return added + [row for row in rows if row["name"] not in names]


@lru_cache(maxsize=1024)
def _regex(pattern: str) -> re.Pattern:
    return re.compile(pattern, re.MULTILINE)


def detection_window(text: str) -> str:
    """First WINDOW_LINES lines, plus any XML namespace declarations (design 5.4): a
    namespace such as 'urn:ietf:params:xml:ns:yang:ietf-system' can sit deep in a document
    but is as strong a vendor signal as a header line."""
    head = "\n".join(text.splitlines()[:WINDOW_LINES])
    ns = [f"{prefix or ''}{uri}" for prefix, uri in _XMLNS.findall(text)]
    return head if not ns else head + "\n" + "\n".join(ns)


def score(text: str, row: dict) -> float:
    return sum(fp.get("weight", 1) for fp in row.get("fingerprints") or ()
               if _regex(fp["pattern"]).search(text))


def resolve_profile(text: str, name: str | None = None,
                    profiles: list[dict] | None = None) -> dict:
    rows = load_profiles() if profiles is None else profiles
    if name:
        for row in rows + [GENERIC]:
            if row["name"] == name:
                return row
        raise ValueError(f"unknown profile {name!r}")
    window = detection_window(text)
    best, best_score = GENERIC, 0.0
    for row in rows:
        s = score(window, row)
        if s > best_score and s >= row.get("threshold", 1):
            best, best_score = row, s
    return best
