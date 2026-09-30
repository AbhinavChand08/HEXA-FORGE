"""Match flat records against a vendor's templates using the trie index (F1).

Tie-breaking (same score): most-literals pattern wins (highest score already encodes
this because kw=+10 >> typed=+3 >> word=+1).  On equal scores, own vendor beats
inherited parent: iterate lineage most-specific first and keep the highest score.
"""
from __future__ import annotations

from app.mapping.compiler import _walk
from app.templating.lexer import Lexer
from app.templating.signature import extract_values


def match_config(cmds, index: dict, profile, lexer: Lexer):
    """Yield (command, template | None, slot values, orphan) in config order.
    profile: vendor id, or its lineage [vendor, parent, ...]: inherited templates are
    candidates too, and on an equal score the more specific profile's template wins.
    orphan: the parent block is unknown, so the child cannot be matched."""
    lineage = [profile] if profile is None or isinstance(profile, str) else list(profile)
    matched: dict[int, str | None] = {}
    for c in cmds:
        if c.parent_id is not None and matched.get(c.parent_id) is None:
            matched[c.id] = None
            yield c, None, [], True
            continue
        ptid = matched.get(c.parent_id) if c.parent_id is not None else None
        tokens = c.tokens
        types = lexer.types(tokens, c.quoted, c.roles)
        structured = c.roles is not None

        best: dict | None = None
        best_score = -1
        for fam in lineage:
            root = index.get((fam, ptid))
            if root is None:
                continue
            for t, s in _walk(root, tokens, types, c.roles, 0, 0, structured):
                # lineage order: first match at a given score wins (own vendor < parent)
                if s > best_score:
                    best, best_score = t, s

        matched[c.id] = best["id"] if best else None
        yield c, best, extract_values(tokens, best["pattern"]) if best else [], False
