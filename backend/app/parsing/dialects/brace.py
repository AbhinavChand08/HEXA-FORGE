"""Brace blocks { } (F5, Juniper, VyOS ...)."""
from __future__ import annotations

from app.core.models import FlatCommand

# Junos: 'inactive: system { ... }' / 'protect: interfaces ...' disables a statement
# without deleting it; the marker is stripped and kept as a flag (design section 6.3).
INACTIVE_MARKERS = {"inactive:", "protect:"}


def _brace_tokens(text: str):
    """Yield (kind, value, line). kinds: word qword { } ; [ ] nl"""
    i, line, n, last = 0, 1, len(text), None
    while i < n:
        ch = text[i]
        if ch == "\n":
            yield ("nl", None, line)
            line, i, last = line + 1, i + 1, "nl"
            continue
        if ch.isspace():
            i += 1
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            end = n if end < 0 else end + 2
            line += text.count("\n", i, end)
            i = end
            continue
        if ch == "#" and last in (None, "nl", ";", "{", "}"):
            while i < n and text[i] != "\n":
                i += 1
            continue
        if ch in "{};[]":
            yield (ch, ch, line)
            i, last = i + 1, ch
            continue
        if ch == '"':
            start_line, j, buf = line, i + 1, []
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n:
                    buf.append(text[j + 1])
                    j += 2
                    continue
                if text[j] == "\n":
                    line += 1
                buf.append(text[j])
                j += 1
            yield ("qword", "".join(buf), start_line)
            i, last = j + 1, "word"
            continue
        j = i
        while j < n and not text[j].isspace() and text[j] not in '{};[]"':
            j += 1
        yield ("word", text[i:j], line)
        i, last = j, "word"


def parse_brace(text: str, hints) -> list[FlatCommand]:
    out: list[FlatCommand] = []
    stack: list[list] = []   # [cmd, index_in_out, open_line, has_child_block]
    pending: list = []       # (token, quoted) or list of such (for [ ... ])
    pending_line = None
    complete = False
    in_list = None
    pending_inactive = False

    def parent():
        return stack[-1][0] if stack else None

    def expand():
        """'name-server [ a b ]' -> two commands."""
        for idx, part in enumerate(pending):
            if isinstance(part, list):
                head = pending[:idx]
                tail = [p for p in pending[idx + 1:] if not isinstance(p, list)]
                return [head + [item] + tail for item in part] or [head + tail]
        return [list(pending)]

    def flush():
        nonlocal pending, pending_line, complete, pending_inactive
        if pending:
            for parts in expand():
                toks = [t for t, _ in parts]
                flags = [q for _, q in parts]
                out.append(FlatCommand(toks, pending_line, parent(), quoted=flags,
                                       inactive=pending_inactive))
        pending, pending_line, complete, pending_inactive = [], None, False, False

    for kind, value, line in _brace_tokens(text):
        if in_list is not None:
            if kind in ("word", "qword"):
                in_list.append((value, kind == "qword"))
            elif kind == "]":
                pending.append(in_list)
                in_list = None
            continue
        if kind in ("word", "qword", "["):
            if complete:
                flush()
            if not pending and kind == "word" and value in INACTIVE_MARKERS:
                pending_inactive, pending_line = True, line
                continue
            if not pending:
                pending_line = line
            if kind == "[":
                in_list = []
            else:
                pending.append((value, kind == "qword"))
        elif kind == "nl":
            if pending:
                complete = True
        elif kind == ";":
            flush()
        elif kind == "{":
            parts = [p for p in pending if not isinstance(p, list)] or [("{block}", False)]
            if stack:
                stack[-1][3] = True
            cmd = FlatCommand([t for t, _ in parts], pending_line or line, parent(),
                              True, quoted=[q for _, q in parts], inactive=pending_inactive)
            out.append(cmd)
            stack.append([cmd, len(out) - 1, line, False])
            pending, pending_line, complete, pending_inactive = [], None, False, False
        elif kind == "}":
            flush()
            if not stack:
                continue
            cmd, idx, open_line, has_child_block = stack.pop()
            children = out[idx + 1:]
            if not children:
                cmd.is_block = False                      # 'foo { }' = presence
            elif not has_child_block and open_line == line:
                # F5 inline list:  servers { 10.1.1.10 10.1.1.11 }
                items = [(t, q) for c in children for t, q in zip(c.tokens, c.quoted)]
                del out[idx:]
                for tok, q in items:
                    out.append(FlatCommand(cmd.tokens + [tok], cmd.src_line, cmd.parent,
                                           quoted=cmd.quoted + [q]))
    flush()
    return out
