"""Rule operators (value operators; present / absent are decided in the evaluator)."""
from __future__ import annotations

import operator


def _norm(x):
    return x.lower() if isinstance(x, str) else x


def _list(x) -> list:
    return [_norm(v) for v in (x if isinstance(x, list) else [x])]


OPS = {
    "eq": lambda a, e: _norm(a) == _norm(e), "ne": lambda a, e: _norm(a) != _norm(e),
    "lt": operator.lt, "le": operator.le, "gt": operator.gt, "ge": operator.ge,
    "between": lambda a, e: e[0] <= a <= e[1],
    "in": lambda a, e: all(v in _list(e) for v in _list(a)),
    "not_in": lambda a, e: not any(v in _list(e) for v in _list(a)),
    "contains": lambda a, e: all(v in _list(a) for v in _list(e)),
    "not_contains": lambda a, e: not any(v in _list(a) for v in _list(e)),
}
