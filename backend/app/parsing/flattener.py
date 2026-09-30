"""Raw config text + detected dialect + vendor profile -> ordered flat records."""
from __future__ import annotations

from app.core.models import SECRET, FlatCommand
from app.parsing.base import DialectHints, effective_hints
from app.parsing.blobs import mask_blobs
from app.parsing.dialects import PARSERS
from app.parsing.secrets import redact


def strip_verbs(commands: list[FlatCommand], hints: DialectHints) -> None:
    """'no ip http server' -> tokens 'ip http server', negated=True (CLI dialects)."""
    verbs = [(p.lower().split(), True) for p in hints.negation_prefixes]
    verbs += [(p.lower().split(), False) for p in hints.affirm_prefixes]
    verbs.sort(key=lambda v: -len(v[0]))
    for cmd in commands:
        if cmd.roles is not None:
            continue
        low = [t.lower() for t in cmd.tokens]
        quoted = cmd.quoted or [False] * len(cmd.tokens)
        for words, negated in verbs:
            n = len(words)
            if words and len(low) > n and low[:n] == words and not any(quoted[:n]):
                cmd.tokens, cmd.quoted, cmd.negated = cmd.tokens[n:], quoted[n:], negated
                break


def detect_device_id(commands: list[FlatCommand], hints: DialectHints) -> str | None:
    keys = {k.lower() for k in hints.hostname_keys}
    for c in commands:
        t = c.tokens
        if (not c.negated and not c.is_block and len(t) >= 2
                and t[-2].lower() in keys and t[-1] != SECRET):
            return t[-1]
    return None


def flatten_config(text: str, dialect: str, profile: dict | None = None,
                   hints: DialectHints | None = None,
                   device_id: str | None = None) -> list[FlatCommand]:
    """Raw config text -> [FlatCommand]. Negation stripped, secrets redacted.

    dialect    from ingest.detector.detect()
    profile    vendor profile row (None = generic knobs only)
    hints      user knobs; values that differ from the defaults beat the profile row
    device_id  default: the hostname found in the config
    """
    text = text.lstrip("\ufeff")
    hints = effective_hints(dialect, profile or {}, hints)
    if hints.redact_secrets:
        text = mask_blobs(text, hints.blob_markers)
    commands = [c for c in PARSERS[dialect](text, hints) if c.tokens]

    next_line = None                        # list items take their first child's line
    for cmd in reversed(commands):
        if cmd.src_line is None:
            cmd.src_line = next_line
        next_line = cmd.src_line
    source = text.splitlines()
    for cmd in commands:
        if not cmd.raw and cmd.src_line and 0 < cmd.src_line <= len(source):
            cmd.raw = source[cmd.src_line - 1].strip()

    strip_verbs(commands, hints)
    if hints.redact_secrets:
        redact(commands, hints)

    for i, cmd in enumerate(commands):
        cmd.id = i
    for cmd in commands:
        cmd.parent_id = cmd.parent.id if cmd.parent is not None else None
        chain, p = [], cmd.parent
        while p is not None:
            chain.append(list(p.tokens))
            p = p.parent
        cmd.context = chain[::-1]

    device_id = device_id or detect_device_id(commands, hints)
    for cmd in commands:
        cmd.device_id = device_id
    return commands
