"""Set-style: one statement per line (Junos / VyOS / PAN-OS set format).

Lines are already flat, so the hierarchy a brace-format parser would show ('system {
services { ssh { ... } } }') has to be inferred (design section 6.3): a leading run of
tokens shared by many lines is a block, the remainder is the leaf statement. This makes
'set system services ssh protocol-version v2' produce parent block records ('system',
'services', 'ssh') plus a leaf ('protocol-version v2') the same shape a brace parser of
the same vendor would give, and 'delete system services telnet' shares the 'system' /
'services' blocks with it (verbs are stripped before the tree is built)."""
from __future__ import annotations

from app.core.models import FlatCommand
from app.parsing.lines import logical_lines

MIN_SHARED = 2   # a prefix must be common to at least this many lines to become a block


def _split_verb(tokens: list[str], hints) -> tuple[bool, int]:
    """-> (negated, number of leading tokens that are the verb)."""
    low = [t.lower() for t in tokens]
    verbs = ([(p.lower().split(), True) for p in hints.negation_prefixes]
             + [(p.lower().split(), False) for p in hints.affirm_prefixes])
    for words, negated in sorted(verbs, key=lambda v: -len(v[0])):
        n = len(words)
        if words and low[:n] == words:
            return negated, n
    return False, 0


def _trie(rests: list[list[str]]) -> dict:
    root = {"count": 0, "children": {}}
    for toks in rests:
        node = root
        node["count"] += 1
        for t in toks:
            node = node["children"].setdefault(t, {"count": 0, "children": {}})
            node["count"] += 1
    return root


def _boundary(rest: list[str], root: dict) -> int:
    """How many of the leading tokens of `rest` are a block shared with other lines: extend
    as long as the NEXT token's node is itself shared by >= MIN_SHARED lines (a leaf keyword
    that merely has several possible values, like 'protocol-version', has a child count of 1
    and stops the walk)."""
    node, i = root, 0
    while i < len(rest) - 1:
        child = node["children"].get(rest[i])
        if child is None or child["count"] < MIN_SHARED:
            break
        node, i = child, i + 1
    return i


def parse_set(text: str, hints) -> list[FlatCommand]:
    lines = logical_lines(text, hints)
    split = []
    for ln in lines:
        negated, n = _split_verb(ln.tokens, hints)
        quoted = ln.quoted or [False] * len(ln.tokens)
        split.append((ln, negated, ln.tokens[n:], quoted[n:]))

    root = _trie([rest for _, _, rest, _ in split])
    out: list[FlatCommand] = []
    blocks: dict[tuple, FlatCommand] = {}
    for ln, negated, rest, quoted in split:
        if not rest:
            continue
        depth = _boundary(rest, root)
        parent = None
        for i in range(depth):
            key = tuple(rest[: i + 1])
            block = blocks.get(key)
            if block is None:
                block = FlatCommand([rest[i]], ln.line_no, parent, True)
                blocks[key] = block
                out.append(block)
            parent = block
        out.append(FlatCommand(rest[depth:], ln.line_no, parent, False, ln.raw,
                               quoted=quoted[depth:], negated=negated,
                               src_lines=ln.src_lines, blob=ln.blob))
    return out
