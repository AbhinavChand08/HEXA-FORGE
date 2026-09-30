"""Token types and shape tests (no regex, no vendor knowledge)."""
from __future__ import annotations

import ipaddress

STRUCTURED = {"xml", "json", "yaml", "ini"}
TYPED = {"SECRET", "CIDR", "MASK", "IPV4", "IPV6", "MAC", "TIME", "IFNAME", "NUM"}


def ip(tok: str):
    try:
        return ipaddress.ip_address(tok)
    except ValueError:
        return None


def mask_kind(bits: int) -> str | None:
    """'net' for 255.255.255.0 style, 'wild' for 0.0.0.255 style, else None."""
    inv = bits ^ 0xFFFFFFFF
    if bits and inv & (inv + 1) == 0:
        return "net"
    if bits & (bits + 1) == 0:
        return "wild"
    return None


def is_mac(tok: str) -> bool:
    t = tok.lower()
    for sep, size in ((":", 2), ("-", 2), (".", 4)):     # aa:bb:.. / aa-bb-.. / aabb.ccdd.eeff
        parts = t.split(sep)
        if len(parts) == 12 // size and all(
                len(p) == size and all(c in "0123456789abcdef" for c in p) for p in parts):
            return True
    return False


def is_time(tok: str) -> bool:
    parts = tok.split(":")
    if len(parts) not in (2, 3) or not all(p.isdigit() for p in parts[:2]):
        return False
    whole, _, frac = (parts[2] if len(parts) == 3 else "0").partition(".")
    if not whole.isdigit() or (frac and not frac.isdigit()):
        return False
    return len(parts[1]) == 2 and int(parts[0]) < 24 and int(parts[1]) < 60 and int(whole) < 60


def is_num(tok: str) -> bool:
    t = tok[1:] if len(tok) > 1 and tok[0] in "+-" else tok
    whole, dot, frac = t.partition(".")
    return whole.isdigit() and (not dot or frac.isdigit())


def keyword_shaped(tok: str) -> bool:
    """Looks like a CLI command word: lowercase letters, digits, hyphens."""
    return (bool(tok) and tok[0].isalpha() and tok[0].islower()
            and all(c.islower() or c.isdigit() or c == "-" for c in tok))


def enum_shaped(tok: str) -> bool:
    """RO, informational, rapid-pvst, Enable - a choice word, not a name like ACL-IN."""
    if not tok or not tok[0].isalpha():
        return False
    return tok.isalpha() or all(c.islower() or c == "-" for c in tok)
