"""Tree formats (XML / JSON / YAML): each key or element becomes a path element; list
items are keyed by their name, so paths stay stable ('interfaces eth0', not 'interfaces 3')."""
from __future__ import annotations

import bisect
import json
import xml.parsers.expat

from app.core.models import FlatCommand

try:
    import yaml  # only needed for YAML configs
except ImportError:  # pragma: no cover
    yaml = None


class LineLocator:
    """Approximate line numbers for keys in JSON / YAML."""

    def __init__(self, text: str):
        self.text = text
        self.starts = [0] + [i + 1 for i, c in enumerate(text) if c == "\n"]
        self.cursor = 0

    def find(self, needle: str) -> int | None:
        pos = self.text.find(needle, self.cursor)
        if pos < 0:
            return None
        self.cursor = pos + len(needle)
        return bisect.bisect_right(self.starts, pos)


def _scalar(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    return str(value)


def _list_key(item: dict, key_fields: list[str]):
    """The field that names a list item ({'name': 'eth0', ...}), if any."""
    by_lower = {str(k).lower(): k for k in item}
    for want in key_fields:
        k = by_lower.get(want.lower())
        if k is not None and item[k] is not None and not isinstance(item[k], (dict, list)):
            return k
    return None


def _walk_member(label, roles, value, parent, out, locate, line, keys):
    if isinstance(value, dict) and value:
        cmd = FlatCommand(label, line, parent, True, roles=roles)
        out.append(cmd)
        _walk_tree(value, cmd, out, locate, keys)
    elif isinstance(value, list) and value:
        _walk_items(label, roles, value, parent, out, locate, line, keys)
    elif isinstance(value, (dict, list)):                 # empty container
        out.append(FlatCommand(label, line, parent, roles=roles))
    else:
        out.append(FlatCommand(label + [_scalar(value)], line, parent,
                               roles=roles + ["value"]))


def _walk_items(label, roles, items, parent, out, locate, line, keys):
    # keyed items give stable paths: 'interfaces eth0 > mtu 9000', not 'interfaces 3 > ...'
    for idx, item in enumerate(items):
        k = _list_key(item, keys) if isinstance(item, dict) else None
        if k is not None:
            rest = {kk: v for kk, v in item.items() if kk != k}
            _walk_member(label + [_scalar(item[k])], roles + ["name"], rest, parent,
                         out, locate, None, keys)
        elif isinstance(item, (dict, list)):
            _walk_member(label + [str(idx)], roles + ["index"], item, parent,
                         out, locate, None, keys)
        else:
            out.append(FlatCommand(label + [_scalar(item)], line, parent,
                                   roles=roles + ["value"]))


def _walk_tree(node, parent, out, locate, keys):
    if isinstance(node, dict):
        for key, value in node.items():
            key = str(key)
            _walk_member([key], ["key"], value, parent, out, locate, locate(key), keys)
    elif isinstance(node, list):
        _walk_items([], [], node, parent, out, locate, None, keys)


def parse_json(text: str, hints) -> list[FlatCommand]:
    loc, out = LineLocator(text), []
    _walk_tree(json.loads(text), None, out, lambda k: loc.find(json.dumps(k)),
               hints.list_key_fields)
    return out


def parse_yaml(text: str, hints) -> list[FlatCommand]:
    loc, out = LineLocator(text), []
    _walk_tree(yaml.safe_load(text), None, out, lambda k: loc.find(f"{k}:"),
               hints.list_key_fields)
    return out


class _XNode:
    __slots__ = ("tag", "attrs", "children", "text", "line")

    def __init__(self, tag, attrs, line):
        self.tag, self.attrs, self.line = tag, attrs, line
        self.children, self.text = [], []

    @property
    def value(self) -> str:
        return " ".join("".join(self.text).split())


def _xml_tree(text: str) -> _XNode:
    parser = xml.parsers.expat.ParserCreate()
    stack: list[_XNode] = []
    roots: list[_XNode] = []

    def start(tag, attrs):
        node = _XNode(tag, attrs, parser.CurrentLineNumber)
        (stack[-1].children if stack else roots).append(node)
        stack.append(node)

    def end(tag):
        stack.pop()

    def chars(data):
        if stack:
            stack[-1].text.append(data)

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = chars
    parser.Parse(text, True)
    return roots[0]


def _xml_key(node: _XNode, key_fields: list[str]):
    """Identity of an element: <entry name="x"> or a <Name>x</Name> child."""
    for want in (k.lower() for k in key_fields):
        for attr, value in node.attrs.items():
            if attr.lower() == want and value.strip():
                return value.strip(), attr
        for child in node.children:
            if (child.tag.lower() == want and not child.children and not child.attrs
                    and child.value):
                return child.value, child
    return None, None


def parse_xml(text: str, hints) -> list[FlatCommand]:
    out: list[FlatCommand] = []

    def emit(node: _XNode, parent, detached=False):
        key, source = (None, None) if detached else _xml_key(node, hints.list_key_fields)
        tokens, roles = [node.tag], ["key"]
        if key is not None:
            tokens.append(key)
            roles.append("name")
        for attr, value in node.attrs.items():
            if value.strip() and attr != source:       # skips e.g. Sophos transactionid=""
                tokens += [attr, value]
                roles += ["key", "value"]
        kids = [c for c in node.children if c is not source]
        if not node.children and node.value and not detached:
            tokens.append(node.value)
            roles.append("value")
        cmd = FlatCommand(tokens, node.line, parent, bool(kids) and not detached, roles=roles)
        out.append(cmd)
        # the root element is kept as one command, but its children are top-level
        for child in kids:
            emit(child, None if detached else cmd)

    emit(_xml_tree(text), None, detached=not hints.xml_root_as_context)
    return out
