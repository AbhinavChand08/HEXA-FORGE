"""Templates -> trie-based lookup index for the matcher (F1).

Each (vendor_family, parent_signature) group gets its own trie root. Walk order
per token position: exact keyword → typed slot → word/enum slot → terminal (list/text).
Serialised as a plain dict tree; no external deps.
"""
from __future__ import annotations

from app.templating.token_types import TYPED


class _Node:
    """Trie node.  All fields are plain dicts / lists so the trie is picklable."""
    __slots__ = ("literals", "typed", "word", "terminal", "leaves")

    def __init__(self):
        self.literals: dict[str, "_Node"] = {}
        self.typed: dict[str, "_Node"] = {}
        self.word: "_Node | None" = None
        # each entry: (template, kind, val) for list/text terminal patterns
        self.terminal: list[tuple] = []
        # templates whose pattern ends exactly here
        self.leaves: list[dict] = []


def _insert(root: _Node, t: dict) -> None:
    node = root
    for kind, val in t["pattern"]:
        if kind in ("list", "text"):
            node.terminal.append((t, kind, val))
            return
        if kind == "kw":
            if val not in node.literals:
                node.literals[val] = _Node()
            node = node.literals[val]
        elif val in TYPED:
            if val not in node.typed:
                node.typed[val] = _Node()
            node = node.typed[val]
        else:                            # WORD / ENUM / QSTR / TEXT
            if node.word is None:
                node.word = _Node()
            node = node.word
    node.leaves.append(t)


def _walk(node: _Node, tokens, types, roles, pos: int, score: int,
          structured: bool) -> list[tuple[dict, int]]:
    """DFS walk. Returns [(template, score), ...] for all matches from this position."""
    if pos == len(tokens):
        return [(t, score) for t in node.leaves if t["structured"] == structured]

    tok = tokens[pos]
    typ = types[pos]
    role = roles[pos] if roles is not None else None
    results: list[tuple[dict, int]] = []

    # terminal patterns: consume all remaining tokens
    if node.terminal:
        for t, kind, val in node.terminal:
            if t["structured"] != structured:
                continue
            if kind == "list" and val in TYPED and any(ty != val for ty in types[pos:]):
                continue
            results.append((t, score + 1))
        return results

    # 1. Exact keyword — in structured configs must be a key position
    if tok in node.literals and (role is None or role == "key"):
        results += _walk(node.literals[tok], tokens, types, roles, pos + 1, score + 10, structured)

    # 2. Typed slot — in structured configs must NOT be a key position
    if typ in TYPED and typ in node.typed and role != "key":
        results += _walk(node.typed[typ], tokens, types, roles, pos + 1, score + 3, structured)

    # 3. Word / enum / qstr slot — must not be a key position in structured configs
    if node.word is not None and role != "key":
        results += _walk(node.word, tokens, types, roles, pos + 1, score + 1, structured)

    return results


def build_index(templates) -> dict:
    """{(vendor_family, parent_signature): _Node}.  Patterns are normalised to tuples."""
    tries: dict[tuple, _Node] = {}
    for t in templates:
        t["pattern"] = tuple(tuple(el) for el in t["pattern"])
        key = (t["vendor_family"], t["parent"])
        if key not in tries:
            tries[key] = _Node()
        _insert(tries[key], t)
    return tries
