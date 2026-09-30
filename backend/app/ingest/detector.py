"""Dialect detection, and the combined vendor + dialect decision for one config."""
from __future__ import annotations

import json
import xml.parsers.expat

from app.ingest.fingerprints import GENERIC, detection_window, load_profiles, resolve_profile, score

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

SET_VERBS = {"set", "delete", "unset", "activate", "deactivate"}
DIALECTS = ("indent", "brace", "set", "xml", "json", "yaml", "ini")
# design 5.4: a job whose top two scores are this close is ambiguous (NEEDS_VENDOR)
AMBIGUOUS_MARGIN = 0.10
# the learned top-level signature counts for about one strong fingerprint when it fully
# overlaps, so a good fingerprint match is not flipped by a coincidental word overlap
OVERLAP_WEIGHT = 3.0


def _looks_set(text: str) -> bool:
    total = hits = 0
    for line in text.splitlines():
        s = line.strip()
        if not s or s[0] in "#!;" or s.startswith("//"):
            continue
        total += 1
        if not line[:1].isspace() and s.split()[0].lower() in SET_VERBS:
            hits += 1
    return total > 0 and hits / total >= 0.8


def _looks_ini(text: str) -> bool:
    total = sections = pairs = 0
    for line in text.splitlines():
        s = line.strip()
        if not s or s[0] in "#;":
            continue
        total += 1
        if s.startswith("[") and s.endswith("]"):
            sections += 1
        elif "=" in s:
            key = s.split("=", 1)[0].strip()
            pairs += bool(key) and not any(c.isspace() for c in key)
    body = total - sections
    return sections > 0 and body > 0 and pairs / body >= 0.6


def _looks_brace(text: str) -> bool:
    opens = closes = 0
    for line in text.splitlines():
        s = line.strip()
        if s.endswith("{"):
            opens += 1
        if s.startswith("}"):
            closes += 1
    return opens >= 2 and closes >= 2 and abs(opens - closes) <= max(2, opens // 10)


def _looks_yaml(text: str) -> bool:
    lines = [l.strip() for l in text.splitlines()
             if l.strip() and not l.strip().startswith("#")]
    if not lines:
        return False
    keyish = sum(1 for l in lines if l.startswith("- ") or l.endswith(":") or ": " in l)
    if keyish / len(lines) < 0.6:
        return False
    try:
        return isinstance(yaml.safe_load(text), (dict, list))
    except yaml.YAMLError:
        return False


def detect_dialect(text: str) -> str:
    s = text.lstrip("\ufeff").lstrip()
    if s[:1] in "{[":
        try:
            json.loads(s)
            return "json"
        except ValueError:
            pass
    if s.startswith("<"):
        try:
            xml.parsers.expat.ParserCreate().Parse(s, True)
            return "xml"
        except xml.parsers.expat.ExpatError:
            pass
    if _looks_brace(s):
        return "brace"
    if _looks_ini(s):
        return "ini"
    if _looks_set(s):
        return "set"
    if yaml is not None and _looks_yaml(s):
        return "yaml"
    return "indent"


def detect(text: str, profiles: list[dict] | None = None, name: str | None = None,
           dialect: str | None = None) -> tuple[dict, str]:
    """-> (vendor profile row, dialect). name / dialect force the choice."""
    profile = resolve_profile(text, name, profiles)
    return profile, dialect or profile.get("dialect") or detect_dialect(text)


def detect_vendor(text: str, profiles: list[dict] | None = None, declared: str | None = None,
                  sigs: dict | None = None) -> dict:
    """The job-facing vendor decision (design 5.4-5.6): weighted fingerprints over
    detection_window(text), blended with the learned top-level command overlap
    (task D2: the same signal `templating.signature.choose_profile` used as a lone
    fallback) as one more weighted vote, then classified:

      MATCHED        the top score clears its profile's threshold, with no close rival
      NEEDS_VENDOR    the top two scores are within AMBIGUOUS_MARGIN of each other
      UNKNOWN_VENDOR  nothing clears its threshold

    declared overrides the pick (its profile is used as-is) but is still checked for a
    fingerprint disagreement (`mismatch`). sigs: {vendor id: (dialect, learned top-level
    signature)} from registry.profile_signatures, for the overlap signal; omit it to score
    fingerprints alone.
    -> {profile, dialect, status, scores: [{name, score}, ...] (top 5, highest first),
        declared, mismatch}
    """
    from app.parsing.base import DialectHints
    from app.parsing.flattener import flatten_config
    from app.templating.signature import file_signature, overlap

    rows = load_profiles() if profiles is None else profiles
    dialect = detect_dialect(text)
    sigs = sigs or {}
    sig = file_signature(dialect, flatten_config(text, dialect, GENERIC, DialectHints())) \
        if sigs else set()
    window = detection_window(text)

    scored = []
    for row in rows:
        s = score(window, row)
        lsig = sigs.get(row["name"])
        if lsig and lsig[0] == dialect:
            s += OVERLAP_WEIGHT * overlap(sig, lsig[1])
        if s > 0:
            scored.append((row, s))
    scored.sort(key=lambda rs: rs[1], reverse=True)
    top, top_score = scored[0] if scored else (GENERIC, 0.0)
    second_score = scored[1][1] if len(scored) > 1 else 0.0
    scores = [{"name": r["name"], "score": round(s, 2)} for r, s in scored[:5]]
    passes = top_score > 0 and top_score >= top.get("threshold", 1)

    if declared:
        chosen = next((r for r in rows if r["name"] == declared), GENERIC)
        return {"profile": chosen, "dialect": chosen.get("dialect") or dialect,
                "status": "MATCHED", "scores": scores, "declared": declared,
                "mismatch": passes and top["name"] != declared}
    if not passes:
        return {"profile": GENERIC, "dialect": dialect, "status": "UNKNOWN_VENDOR",
                "scores": scores, "declared": None, "mismatch": None}
    if second_score > 0 and top_score - second_score <= AMBIGUOUS_MARGIN * top_score:
        return {"profile": top, "dialect": top.get("dialect") or dialect,
                "status": "NEEDS_VENDOR", "scores": scores, "declared": None, "mismatch": None}
    return {"profile": top, "dialect": top.get("dialect") or dialect, "status": "MATCHED",
            "scores": scores, "declared": None, "mismatch": None}
