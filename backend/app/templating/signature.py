"""Template rendering, stable signatures, slot values, and top-level file signatures."""
from __future__ import annotations

import hashlib
import json

from app.templating.token_types import STRUCTURED, keyword_shaped


def render(pattern) -> str:
    return " ".join(val if kind == "kw" else f"<{val}...>" if kind == "list" else f"<{val}>"
                    for kind, val in pattern)


def signature(vendor_family: str, dialect: str, path: list[str]) -> str:
    """hash(vendor_family, dialect, template_path) - the stable template id."""
    return hashlib.sha256(json.dumps([vendor_family, dialect, path]).encode()).hexdigest()[:16]


def extract_values(tokens, pattern) -> list[str]:
    vals, i = [], 0
    for kind, _ in pattern:
        if kind == "kw":
            i += 1
        elif kind == "slot":
            vals.append(tokens[i] if i < len(tokens) else "")
            i += 1
        else:
            vals.append(" ".join(tokens[i:]))
            break
    return vals


def overlap(a, b) -> float:
    """Overlap coefficient: a small config that is a subset of a big one scores 1."""
    return len(a & b) / min(len(a), len(b)) if (a and b) else 0.0


def file_signature(dialect: str, commands) -> set[str]:
    """Top-level command words of one config, used to route it to a vendor family."""
    sig = set()
    for c in commands:
        if c.parent_id is not None:
            continue
        t = c.tokens
        if dialect in STRUCTURED or len(t) < 2 or not keyword_shaped(t[1]):
            sig.add(t[0])
        else:
            sig.add(f"{t[0]} {t[1]}")
    return sig


def choose_profile(dialect: str, cmds, vendor: str, sigs: dict,
                   min_overlap: float = 0.0) -> str | None:
    """sigs: {profile: (dialect, top-level signature)}. None when nothing is close enough."""
    if vendor in sigs:
        return vendor
    sig = file_signature(dialect, cmds)
    ranked = sorted(sigs, key=lambda n: (sigs[n][0] == dialect, overlap(sig, sigs[n][1])),
                    reverse=True)
    if ranked and overlap(sig, sigs[ranked[0]][1]) >= min_overlap:
        return ranked[0]
    return None
