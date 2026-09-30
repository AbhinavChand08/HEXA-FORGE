"""The sandboxed expression language used by mappings.

    min * 60 + sec                          Cisco exec-timeout <min> <sec> -> seconds
    to_seconds(v1, 'min')                   FortiOS admintimeout (minutes) -> seconds
    int(strip_prefix(v, 'v'))               Junos protocol-version v2 -> 2
    not negated                             'no ip http server' -> False
    syslog_level(v1)                        'informational' -> 6
    enum_map(v1, {'RO': 'read', 'RW': 'write'})
    tokens[2:]                              'transport input ssh telnet' -> ['ssh', 'telnet']

Only whitelisted functions, no attribute access, no imports, no loops (comprehensions),
bounded length, bounded integer size. Every operation is then cheap, so evaluation cost is
bounded by the expression length (a wall-clock timer could not interrupt one big C-level
operation such as 9 ** 4000000 anyway; the bounds forbid it).
Variables: v1..vn (every slot of the template path, parents first; NUM slots are
numbers), the mapping's slot names, negated, present, tokens, raw, values, blob.
"""
from __future__ import annotations

import ast
import ipaddress
import re

from simpleeval import DEFAULT_OPERATORS, EvalWithCompoundTypes

from app.core.errors import TransformError
from app.mapping.transforms.functions import FUNCTIONS, boolean, number, to_int

MAX_EXPR = 400
MAX_EXPONENT = 64
MAX_INT_BITS = 4096
RESERVED = {"negated", "present", "tokens", "raw", "values", "blob"}
FORBIDDEN = (ast.Attribute, ast.Lambda, ast.NamedExpr, ast.ListComp, ast.SetComp, ast.DictComp,
             ast.GeneratorExp)
CONSTANTS = {"True": True, "False": False, "None": None}


def _pow(a, b):
    if abs(number(b)) > MAX_EXPONENT or (
            isinstance(a, int) and isinstance(b, int) and b > 0
            and a.bit_length() * b > MAX_INT_BITS):
        raise TransformError(f"{a!r} ** {b!r} is too large")
    return a ** b


def _lshift(a, b):
    if not isinstance(a, int) or not isinstance(b, int) or b > MAX_INT_BITS \
            or a.bit_length() + b > MAX_INT_BITS:
        raise TransformError(f"{a!r} << {b!r} is too large")
    return a << b


OPERATORS = {**DEFAULT_OPERATORS, ast.Pow: _pow, ast.LShift: _lshift}


# =============================================================================
# Compile-time check and evaluation
# =============================================================================
def free_names(expr: str) -> set[str]:
    return {n.id for n in ast.walk(_parse(expr)) if isinstance(n, ast.Name)}


def _parse(expr: str) -> ast.Expression:
    if not isinstance(expr, str) or not expr.strip():
        raise TransformError("empty expression")
    if len(expr) > MAX_EXPR:
        raise TransformError(f"expression longer than {MAX_EXPR} characters")
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise TransformError(f"syntax error: {e.msg}") from None
    for node in ast.walk(tree):
        if isinstance(node, FORBIDDEN):
            raise TransformError(f"{type(node).__name__} is not allowed")
    return tree


def check(expr: str, variables) -> None:
    """Reject syntax errors, attribute access and names that are not variables."""
    unknown = free_names(expr) - set(variables) - FUNCTIONS.keys() - CONSTANTS.keys()
    if unknown:
        raise TransformError(f"unknown name(s): {', '.join(sorted(unknown))}")


class _Rename(ast.NodeTransformer):
    def __init__(self, mapping):
        self.mapping = mapping

    def visit_Call(self, node):                     # function names are not variables
        node.args = [self.visit(a) for a in node.args]
        node.keywords = [self.visit(k) for k in node.keywords]
        return node

    def visit_Name(self, node):
        return ast.copy_location(ast.Name(self.mapping.get(node.id, node.id), node.ctx), node)


def rename(expr: str, mapping: dict) -> str:
    """Rebind variables: rename('min * 60 + sec', {'min': 'v3', 'sec': 'v4'}) -> 'v3 * 60 + v4'"""
    return ast.unparse(_Rename(mapping).visit(_parse(expr)))


def evaluate(expr: str, variables: dict):
    _parse(expr)
    ev = EvalWithCompoundTypes(operators=OPERATORS, functions=FUNCTIONS,
                               names={**CONSTANTS, **variables})
    try:
        return ev.eval(expr)
    except TransformError:
        raise
    except Exception as e:  # simpleeval raises its own errors plus the operators' ones
        raise TransformError(f"{type(e).__name__}: {e}") from None


def slot_value(value: str, slot_type: str):
    """NUM slots become numbers so 'min * 60 + sec' works; everything else stays text."""
    if slot_type == "NUM":
        try:
            return number(value)
        except TransformError:
            return value
    return value


def variables(values, slot_types, slot_names=(), negated=False, tokens=(), raw="",
              blob=None) -> dict:
    typed = [slot_value(v, t) for v, t in zip(values, slot_types)]
    out = {f"v{i + 1}": v for i, v in enumerate(typed)}
    for name, v in zip(slot_names, typed):
        if name:
            out[name] = v
    out.update(negated=bool(negated), present=True, tokens=list(tokens), raw=raw, values=typed,
              blob=blob)
    return out


def variable_names(n_slots: int, slot_names=()) -> set[str]:
    return {f"v{i + 1}" for i in range(n_slots)} | {n for n in slot_names if n} | RESERVED


_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def render_scope(fmt: str, names: dict) -> str:
    """'con {lno}' -> 'con 0'. Plain substitution only (no str.format attribute access)."""
    def sub(m):
        if m.group(1) not in names:
            raise TransformError(f"scope id uses unknown slot {m.group(1)!r}")
        return str(names[m.group(1)])
    return _PLACEHOLDER.sub(sub, fmt)


def scope_names(fmt: str) -> set[str]:
    return set(_PLACEHOLDER.findall(fmt or ""))


# =============================================================================
# Datatype coercion against the canonical key
# =============================================================================
def coerce(value, key: dict):
    """Cast a transform result to the key's datatype. Lists only for list keys."""
    if isinstance(value, (list, tuple)):
        if key["cardinality"] != "list":
            raise TransformError(f"{key['key']} is single-valued, got a list")
        return [_coerce_one(v, key) for v in value]
    return _coerce_one(value, key)


def _coerce_one(v, key: dict):
    t = key["type"]
    if v is None:
        raise TransformError("transform returned None")
    if t == "int":
        return to_int(v)
    if t == "float":
        return float(number(v))
    if t == "bool":
        return boolean(v)
    if t == "enum":
        allowed = key.get("enum_values") or []
        for a in allowed:
            if str(a).lower() == str(v).lower():
                return a
        if allowed:
            raise TransformError(f"{v!r} is not one of {allowed} for {key['key']}")
        return str(v)
    if t == "ip":
        try:
            return str(ipaddress.ip_address(str(v)))
        except ValueError:
            raise TransformError(f"not an IP address: {v!r}") from None
    if t == "cidr":
        try:
            return str(ipaddress.ip_network(str(v), strict=False))
        except ValueError:
            raise TransformError(f"not a network: {v!r}") from None
    return str(v)
