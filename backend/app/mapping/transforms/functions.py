"""Transform function library. The only extension point that needs code: every function
here serves every vendor, and each new one needs a unit test."""
from __future__ import annotations

import ipaddress

from app.core.errors import TransformError

SYSLOG_LEVELS = {
    "emergencies": 0, "emergency": 0, "emerg": 0, "alerts": 1, "alert": 1,
    "critical": 2, "crit": 2, "errors": 3, "error": 3, "err": 3,
    "warnings": 4, "warning": 4, "warn": 4, "notifications": 5, "notification": 5,
    "notice": 5, "informational": 6, "information": 6, "info": 6,
    "debugging": 7, "debug": 7}
UNITS = {"ms": 0.001, "s": 1, "sec": 1, "second": 1, "seconds": 1, "m": 60, "min": 60,
         "minute": 60, "minutes": 60, "h": 3600, "hour": 3600, "hours": 3600,
         "d": 86400, "day": 86400, "days": 86400}
TRUE_WORDS = {"enable", "enabled", "on", "yes", "true", "1", "y", "permit", "allow"}
FALSE_WORDS = {"disable", "disabled", "off", "no", "false", "0", "n", "none", "deny"}
_MISSING = object()


def number(x):
    if isinstance(x, bool):
        raise TransformError(f"not a number: {x!r}")
    if isinstance(x, (int, float)):
        return x
    s = str(x).strip()
    try:
        return int(s)
    except ValueError:
        try:
            return float(s)
        except ValueError:
            raise TransformError(f"not a number: {x!r}") from None


def to_int(x):
    v = number(x)
    if isinstance(v, float) and not v.is_integer():
        raise TransformError(f"not an integer: {x!r}")
    return int(v)


def boolean(x):
    if isinstance(x, bool):
        return x
    s = str(x).strip().lower()
    if s in TRUE_WORDS:
        return True
    if s in FALSE_WORDS:
        return False
    raise TransformError(f"not a boolean: {x!r}")


def enum_map(v, table, default=_MISSING):
    for k, out in table.items():
        if str(k).lower() == str(v).lower():
            return out
    if default is _MISSING:
        raise TransformError(f"{v!r} not in enum_map {sorted(map(str, table))}")
    return default


def mask_to_prefix(mask):
    """255.255.255.0 or wildcard 0.0.0.255 -> 24"""
    try:
        return ipaddress.IPv4Network(f"0.0.0.0/{mask}").prefixlen
    except ValueError:
        raise TransformError(f"not a mask: {mask!r}") from None


def strip_prefix(s, prefix):
    s = str(s)
    return s[len(prefix):] if s.startswith(prefix) else s


def strip_suffix(s, suffix):
    s = str(s)
    return s[:-len(suffix)] if suffix and s.endswith(suffix) else s


def presence(x):
    return x is not None and x != "" and x != []


def to_seconds(v, unit="s"):
    u = str(unit).lower()
    if u not in UNITS:
        raise TransformError(f"unknown unit {unit!r}")
    out = number(v) * UNITS[u]
    return int(out) if float(out).is_integer() else out


def syslog_level(v):
    s = str(v).strip().lower()
    if s.isdigit() and int(s) <= 7:
        return int(s)
    if s not in SYSLOG_LEVELS:
        raise TransformError(f"unknown syslog level {v!r}")
    return SYSLOG_LEVELS[s]


def split(s, sep=None):
    return [p.strip() for p in str(s).split(sep) if p.strip()]


def default(x, fallback):
    return fallback if x is None or x == "" else x


FUNCTIONS = {
    "int": to_int, "float": lambda x: float(number(x)), "str": str, "bool": boolean,
    "boolean": boolean, "number": number, "mul": lambda a, b: number(a) * number(b),
    "enum_map": enum_map, "mask_to_prefix": mask_to_prefix, "strip_prefix": strip_prefix,
    "strip_suffix": strip_suffix, "presence": presence, "to_seconds": to_seconds,
    "syslog_level": syslog_level, "split": split, "join": lambda xs, sep=" ": sep.join(map(str, xs)),
    "lower": lambda s: str(s).lower(), "upper": lambda s: str(s).upper(),
    "first": lambda xs: xs[0] if xs else None, "default": default,
    "len": len, "min": min, "max": max, "abs": abs, "round": round,
}
