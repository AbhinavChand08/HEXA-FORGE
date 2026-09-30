"""Indentation + terminators (Cisco, Arista, FortiGate, Huawei, MikroTik ...)."""
from __future__ import annotations

from app.core.models import FlatCommand
from app.parsing.lines import LogicalLine, logical_lines


def _auto_terminators(lines: list[LogicalLine]) -> set[str]:
    occ: dict[str, int] = {}
    ded: dict[str, int] = {}
    prev = None
    for ln in lines:
        if ln.kind == "cmd" and len(ln.tokens) == 1:
            w = ln.tokens[0].lower()
            occ[w] = occ.get(w, 0) + 1
            if prev is not None and ln.indent < prev:
                ded[w] = ded.get(w, 0) + 1
        prev = ln.indent
    return {w for w, c in occ.items() if c >= 2 and ded.get(w, 0) / c >= 0.8}


def parse_indent(text: str, hints) -> list[FlatCommand]:
    lines = logical_lines(text, hints)
    terminators = {t.lower() for t in hints.terminators}
    if hints.auto_detect_terminators:
        terminators |= _auto_terminators(lines)
    indented = any(ln.indent > 0 for ln in lines if ln.kind == "cmd")
    keyword_mode = bool(hints.block_openers) and not indented
    openers = {o.lower() for o in hints.block_openers}

    out: list[FlatCommand] = []
    stack: list[tuple[int, FlatCommand]] = []
    for ln in lines:
        if ln.kind == "section":
            cmd = FlatCommand(ln.tokens, ln.line_no, None, False, ln.raw, quoted=ln.quoted)
            out.append(cmd)
            stack = [(-1, cmd)]
            continue
        if " ".join(ln.tokens).lower() in terminators:
            if keyword_mode and stack and stack[-1][0] >= 0:
                stack.pop()
            continue
        if not keyword_mode:
            while stack and stack[-1][0] >= ln.indent:
                stack.pop()
        parent = stack[-1][1] if stack else None
        if parent is not None:
            parent.is_block = True
        cmd = FlatCommand(ln.tokens, ln.line_no, parent, False, ln.raw, quoted=ln.quoted,
                          src_lines=ln.src_lines, blob=ln.blob)
        out.append(cmd)
        if keyword_mode:
            if ln.tokens and ln.tokens[0].lower() in openers:
                stack.append((0, cmd))
        else:
            stack.append((ln.indent, cmd))
    return out
