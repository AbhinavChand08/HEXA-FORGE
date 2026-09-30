"""Multi-line blobs: PEM keys / certificates, delimited banners, marker-closed blocks."""
from __future__ import annotations

from app.core.models import SECRET

# Cisco 'crypto pki certificate chain':  certificate 01 / <hex lines> / quit
CERT_BLOB = {"start": "certificate", "end": "quit", "mask": True, "indented": True}


def mask_blobs(text: str, markers: list[dict]) -> str:
    """PEM keys / certificates -> <SECRET>. Newlines are kept so line numbers stay right."""
    for m in markers:
        begin, end, close = m["begin"], m["end"], m.get("close", "")
        parts, pos = [], 0
        while True:
            start = text.find(begin, pos)
            stop = text.find(end, start + len(begin)) if start >= 0 else -1
            if stop < 0:
                break
            stop += len(end)
            c = text.find(close, stop) if close else -1
            if c >= 0 and "\n" not in text[stop:c]:
                stop = c + len(close)
            parts += [text[pos:start], SECRET, "\n" * text.count("\n", start, stop)]
            pos = stop
        text = "".join(parts) + text[pos:]
    return text


def literal_start(stripped: str, hints):
    parts = stripped.split()
    if len(parts) < 2:
        return None
    last = parts[-1]
    for delim in sorted(hints.literal_delimiters, key=len, reverse=True):
        if last == delim or (len(delim) >= 2 and last.startswith(delim)):
            return delim, " ".join(parts[:-1]), last[len(delim):]
    return None


def read_delimited(raw: list[str], i: int, stripped: str, hints):
    """banner motd ^C ... ^C. raw[i] is the line after the start line.
    -> (head, body, index of the next line) or None"""
    lit = literal_start(stripped, hints)
    if not lit:
        return None
    delim, head, rest = lit
    if delim in rest:
        return head, rest[:rest.index(delim)].strip(), i
    j, body, n = i, ([rest] if rest else []), len(raw)
    while j < n and j - i < hints.max_literal_lines and delim not in raw[j]:
        body.append(raw[j].strip())
        j += 1
    if j < n and delim in raw[j]:
        body.append(raw[j].split(delim, 1)[0].strip())
        return head, " ".join(b for b in body if b), j + 1
    return None


def read_marker_block(raw: list[str], i: int, indent: int, stripped: str, hints):
    """Block closed by a marker line: 'banner login' ... 'EOF' (Arista), 'certificate 01'
    ... 'quit' (mask: body -> <SECRET>). -> (head, body, index of the next line) or None"""
    words = stripped.split()
    block = next((b for b in hints.literal_blocks
                  if words[:len(b["start"].split())] == b["start"].split()), None)
    if not block:
        return None
    j, n = i, len(raw)
    while j < n and j - i < hints.max_literal_lines and raw[j].strip() != block["end"]:
        if (block.get("indented") and raw[j].strip()
                and len(raw[j]) - len(raw[j].lstrip()) <= indent):
            return None                              # not a blob: body is not nested
        j += 1
    if j < n and raw[j].strip() == block["end"]:
        body = [raw[k].strip() for k in range(i, j) if raw[k].strip()]
        text = (SECRET if body else "") if block.get("mask") else " ".join(body)
        return stripped, text, j + 1
    return None
