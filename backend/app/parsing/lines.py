"""Tokenizing and logical lines (continuations, quoted strings, literal blobs)."""
from __future__ import annotations

from dataclasses import dataclass

from app.parsing.blobs import read_delimited, read_marker_block


def tokenize_q(text: str) -> tuple[list[str], list[bool]]:
    """Whitespace split keeping "quoted strings" together. Returns tokens + quoted flags."""
    tokens, flags, cur = [], [], []
    in_quote = quoted = False
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and in_quote and i + 1 < len(text):
            cur.append(text[i + 1])
            i += 2
            continue
        if ch == '"':
            in_quote, quoted = not in_quote, True
        elif ch.isspace() and not in_quote:
            if cur or quoted:
                tokens.append("".join(cur))
                flags.append(quoted)
                cur, quoted = [], False
        else:
            cur.append(ch)
        i += 1
    if cur or quoted:
        tokens.append("".join(cur))
        flags.append(quoted)
    return tokens, flags


def tokenize(text: str) -> list[str]:
    return tokenize_q(text)[0]


def _is_identifier(s: str) -> bool:
    return bool(s) and s[0].isalpha() and all(c.isalnum() or c in "-_." for c in s)


def split_key_values(tokens, flags):
    """'disabled=yes' -> 'disabled', 'yes' (MikroTik, INI)."""
    out_t, out_f = [], []
    for tok, q in zip(tokens, flags):
        key, sep, value = tok.partition("=")
        if sep and value and not q and _is_identifier(key):
            out_t += [key, value]
            out_f += [False, False]
        else:
            out_t.append(tok)
            out_f.append(q)
    return out_t, out_f


def _unbalanced_quotes(s: str) -> bool:
    count, i = 0, 0
    while i < len(s):
        if s[i] == "\\":
            i += 2
            continue
        if s[i] == '"':
            count += 1
        i += 1
    return count % 2 == 1


@dataclass
class LogicalLine:
    line_no: int
    indent: int
    tokens: list[str]
    quoted: list[bool]
    raw: str
    kind: str = "cmd"          # cmd | section
    blob: str | None = None            # banner / certificate body, kept out of tokens
    src_lines: list[int] | None = None  # every line a multi-line blob spans


def _is_section(stripped: str, hints) -> bool:
    if any(stripped.startswith(p) for p in hints.section_prefixes):
        return True
    return stripped.startswith("[") and stripped.endswith("]") and "=" not in stripped


def _literal_line(line_no, indent, head, body, raw, end_line):
    toks, flags = tokenize_q(head)
    src_lines = list(range(line_no, end_line + 1)) if end_line > line_no else None
    return LogicalLine(line_no, indent, toks, flags, raw, blob=body or None, src_lines=src_lines)


def logical_lines(text: str, hints) -> list[LogicalLine]:
    raw = text.expandtabs(4).splitlines()
    out: list[LogicalLine] = []
    i, n = 0, len(raw)
    while i < n:
        line = raw[i]
        stripped = line.strip()
        line_no = i + 1
        i += 1
        if (not stripped
                or any(stripped.startswith(p) for p in hints.comment_prefixes)
                or any(stripped.startswith(p) for p in hints.ignore_prefixes)):
            continue
        indent = len(line) - len(line.lstrip())

        # 1. literal text: 'banner motd ^C ... ^C', or a block closed by a marker line
        found = (read_delimited(raw, i, stripped, hints)
                 or read_marker_block(raw, i, indent, stripped, hints))
        if found:
            head, body, i = found
            out.append(_literal_line(line_no, indent, head, body, stripped, i))
            continue

        # 2. backslash continuation
        while stripped.endswith("\\") and i < n:
            stripped = stripped[:-1].rstrip() + " " + raw[i].strip()
            i += 1
        # 3. quoted string spanning lines
        while _unbalanced_quotes(stripped) and i < n and i - line_no < hints.max_literal_lines:
            stripped = stripped + " " + raw[i].strip()
            i += 1

        if _is_section(stripped, hints):
            if stripped.startswith("["):
                toks, flags = [stripped[1:-1].strip()], [False]
            else:
                toks, flags = tokenize_q(stripped)
            out.append(LogicalLine(line_no, indent, toks, flags, stripped, "section"))
            continue

        toks, flags = tokenize_q(stripped)
        if hints.split_key_value:
            toks, flags = split_key_values(toks, flags)
        out.append(LogicalLine(line_no, indent, toks, flags, stripped))
    return out
