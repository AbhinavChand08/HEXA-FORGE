"""Scope selection: which scope instances of a normalized document a rule looks at."""
from __future__ import annotations

import re


def selected(o: dict, selector: dict | None) -> bool:
    """selector: {type: LINE, id_regex: "^vty"}; None selects everything."""
    if not selector:
        return True
    if selector.get("type") and o["scope_type"] != selector["type"]:
        return False
    return not selector.get("id_regex") or re.search(selector["id_regex"], o["scope_id"]) is not None
