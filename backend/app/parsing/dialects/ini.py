"""INI-like: [section] + key = value."""
from __future__ import annotations

from app.core.models import FlatCommand
from app.parsing.lines import tokenize


def _unquote(s: str) -> str:
    return s[1:-1] if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'" else s


def parse_ini(text: str, hints) -> list[FlatCommand]:
    """[section] / [section "sub"] + key = value (or key: value). Roles like JSON."""
    out: list[FlatCommand] = []
    section = last = None
    for no, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if not s or any(s.startswith(p) for p in hints.comment_prefixes):
            continue
        if s.startswith("[") and s.endswith("]"):
            inner = s[1:-1].strip()
            toks = tokenize(inner) if '"' in inner else [inner]
            section = FlatCommand(toks, no, None, raw=s,
                                  roles=["key"] + ["name"] * (len(toks) - 1))
            out.append(section)
            last = None
            continue
        cut = min((p for p in (s.find("="), s.find(":")) if p >= 0), default=-1)
        if cut < 0 and last is not None and line[:1].isspace() and last.roles[-1] == "value":
            last.tokens[-1] += " " + s                # continuation line
            continue
        key, value = (s[:cut].strip(), _unquote(s[cut + 1:].strip())) if cut >= 0 else (s, "")
        toks, roles = [key], ["key"]
        if value:
            toks.append(value)
            roles.append("value")
        if section is not None:
            section.is_block = True
        last = FlatCommand(toks, no, section, raw=s, roles=roles)
        out.append(last)
    return out
