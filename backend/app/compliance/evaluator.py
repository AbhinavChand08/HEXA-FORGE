"""
evaluator.py  -  evaluate rules (predefined expected values) against normalised values

PASS / FAIL per scope instance. UNKNOWN when the config gives the key no value and
  - an unreviewed template of the device could still emit it: a ranked candidate of its
    suggestion names the key, or the model has not scored it (it could emit anything), or
    a command matched no template at all; or
  - nothing gives it a value (no config line, no vendor default).
A vendor default only decides a rule when no unreviewed command could override it, so an
incomplete mapping never makes a device look compliant (or non-compliant).
"""
from __future__ import annotations

from collections import defaultdict

from app.compliance.operators import OPS
from app.compliance.scope import selected


def evaluate(doc: dict, rules: list[dict]) -> list[dict]:
    by_key = defaultdict(list)
    for o in doc["values"]:
        by_key[o["key"]].append(o)
    pending = defaultdict(list)                  # key -> unreviewed templates that may emit it
    unscored = 0                                 # unreviewed commands that may emit any key
    for p in doc.get("pending", []):
        if p.get("candidate_keys") is None:
            unscored += 1
        for k in p.get("candidate_keys") or []:
            pending[k].append(p["template_id"])
    unscored += doc.get("summary", {}).get("unrecognized", 0)
    results = []
    for r in rules:
        obs = [o for o in by_key[r["key"]] if selected(o, r["scope_selector"])]
        base = {"rule": r["id"], "key": r["key"], "severity": r["severity"], "title": r["title"],
                "operator": r["operator"], "expected": r["expected"],
                "pending": pending.get(r["key"], [])[:5], "unscored": unscored}
        if not any(o["source"] == "config" for o in obs) and (base["pending"] or unscored):
            results.append({**base, "status": "UNKNOWN", "scope": "", "source": "pending review",
                            "actual": obs[0]["value"] if obs else None, "line": None})
            continue
        if r["operator"] in ("present", "absent"):          # every command reviewed: decidable
            present = any(o["value"] not in (None, "", [], False) for o in obs)
            ok = present if r["operator"] == "present" else not present
            actual = obs[0]["value"] if len(obs) == 1 else [o["value"] for o in obs] or None
            results.append({**base, "status": "PASS" if ok else "FAIL", "scope": "",
                            "actual": actual,
                            "source": ",".join(sorted({o["source"] for o in obs})),
                            "line": next((o.get("line") for o in obs if o.get("line")), None)})
            continue
        if not obs:
            results.append({**base, "status": "UNKNOWN", "scope": "", "actual": None,
                            "source": "", "line": None})
            continue
        for o in obs:
            try:
                status = "PASS" if OPS[r["operator"]](o["value"], r["expected"]) else "FAIL"
            except (TypeError, IndexError, KeyError):
                status = "ERROR"
            results.append({**base, "status": status,
                            "scope": f"{o['scope_type']}:{o['scope_id']}" if o["scope_id"] else "",
                            "actual": o["value"], "source": o["source"], "line": o.get("line")})
    return results
