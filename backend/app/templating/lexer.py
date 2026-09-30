"""Lexer: typed tokens, highest priority first, and runtime typed-only templatization."""
from __future__ import annotations

import ipaddress

from app.core.models import SECRET
from app.parsing.secrets import looks_like_hash
from app.templating.hints import LearnerHints
from app.templating.token_types import ip, is_mac, is_num, is_time, mask_kind


class Lexer:
    def __init__(self, ifname_prefixes: list[str]):
        self.prefixes = {p.lower() for p in ifname_prefixes}

    def is_ifname(self, tok: str) -> bool:
        """GigabitEthernet0/1, ge-0/0/0.0, 10GE1/0/1, Loopback0, Port-channel10 ..."""
        i = 0
        while i < len(tok) and tok[i].isdigit():
            i += 1
        j = i
        while j < len(tok) and (tok[j].isalpha() or tok[j] == "-"):
            j += 1
        stem, rest = tok[:j], tok[j:]
        if j == i or not any(c.isdigit() for c in rest):
            return False
        if not all(c.isdigit() or c in "/:." for c in rest):
            return False
        known = stem.lower().rstrip("-") in self.prefixes
        return (rest[0].isdigit() or (rest[0] == "." and known)) and ("/" in rest or known)

    def type(self, tok: str, quoted: bool = False, prev: str | None = None) -> str | None:
        """Typed class of one token, or None for a plain word."""
        if tok == SECRET or looks_like_hash(tok):
            return "SECRET"
        head = tok.split("/", 1)[0]
        if "/" in tok and ("." in head or ":" in head):
            try:
                ipaddress.ip_network(tok, strict=False)
                return "CIDR"
            except ValueError:
                pass
        addr = ip(tok)
        if addr is not None and addr.version == 4:
            kind = mask_kind(int(addr))
            # 0.0.0.255 / 0.0.0.0 / 255.255.255.255 are masks only right after an address
            if (kind == "net" and tok.startswith("255.") and tok != "255.255.255.255") or (
                    kind and prev == "IPV4"):
                return "MASK"
            return "IPV4"
        if addr is not None:
            return "IPV6"
        if is_mac(tok):
            return "MAC"
        if is_time(tok):
            return "TIME"
        if self.is_ifname(tok):
            return "IFNAME"
        if is_num(tok):
            return "NUM"
        if quoted:
            return "QSTR"
        if any(c.isspace() for c in tok):
            return "TEXT"
        return None

    def types(self, tokens, quoted=None, roles=None) -> list[str | None]:
        """Types for a whole command; structured keys are never typed."""
        out, prev = [], None
        for i, tok in enumerate(tokens):
            if roles is not None and roles[i] == "key":
                t = None
            else:
                t = self.type(tok, bool(quoted and quoted[i]), prev)
            out.append(t)
            prev = t
        return out


def typed_pattern(tokens, types, roles, hints: LearnerHints) -> tuple:
    """Runtime templatization without mining: typed tokens become slots, other words stay
    keywords, except structured values and hinted positions (<WORD>, <TEXT>, lists).
    reserved_keywords (design 7.3) are never a slot, however the lexer or a hint types them."""
    value_after = {w.lower() for w in hints.value_after}
    text_after = {w.lower() for w in hints.text_after}
    list_after = {w.lower() for w in hints.list_after}
    reserved = {w.lower() for w in hints.reserved_keywords}
    out = []
    for i, (tok, typ) in enumerate(zip(tokens, types)):
        prev = out[-1][1].lower() if out and out[-1][0] == "kw" else None
        if roles is None and prev in text_after:
            out.append(("text", "TEXT"))
            break
        if roles is None and prev in list_after:
            rest = set(types[i:])
            out.append(("list", rest.pop() if len(rest) == 1 and None not in rest else "WORD"))
            break
        if roles is None and tok.lower() in reserved:
            out.append(("kw", tok))
        elif typ:
            out.append(("slot", typ))
        elif (roles[i] != "key") if roles is not None else (prev in value_after):
            out.append(("slot", "WORD"))
        else:
            out.append(("kw", tok))
    return tuple(out)
