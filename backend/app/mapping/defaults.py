"""Vendor defaults: what ABSENCE of a command means (design section 11.4 step 5)."""
from __future__ import annotations

import re


def _version_tuple(v) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", str(v))[:4])


def version_in_range(version, spec: str) -> bool:
    """'15.2(4)M' in '>=15.0,<17'"""
    if version is None:
        return False
    have = _version_tuple(version)
    for part in spec.split(","):
        m = re.fullmatch(r"\s*(>=|<=|>|<|==)?\s*([\w.()]+)\s*", part)
        if not m:
            return False
        op, want = m.group(1) or "==", _version_tuple(m.group(2))
        if not {"<": have < want, "<=": have <= want, ">": have > want,
                ">=": have >= want, "==": have[:len(want)] == want}[op]:
            return False
    return True


def apply_defaults(obs: dict, instances: dict, rows: list[dict], keys: dict,
                   rule_keys: set[str] | None = None) -> int:
    """Fill keys still absent from obs with the vendor default (scoped keys: every scope
    instance seen). rows: vendor_default rows; OS-specific rows win.
    rule_keys: when provided, only apply defaults for keys in this set.
    -> number applied"""
    version = obs.get(("SYSTEM.OS_VERSION", "GLOBAL", ""), {}).get("value")
    n = 0
    for d in sorted(rows, key=lambda d: d["os_range"] is None):
        if rule_keys is not None and d["key"] not in rule_keys:
            continue
        if d["os_range"] and not version_in_range(version, d["os_range"]):
            continue
        key = keys[d["key"]]
        targets = ([("GLOBAL", "")] if key["scope"] == "GLOBAL"
                   else [(key["scope"], sid) for sid in sorted(instances[key["scope"]])])
        for stype, sid in targets:
            if (d["key"], stype, sid) not in obs:
                obs[(d["key"], stype, sid)] = {
                    "key": d["key"], "scope_type": stype, "scope_id": sid,
                    "value": d["value"], "source": "default", "default": True,
                    "note": d["note"]}
                n += 1
    return n
