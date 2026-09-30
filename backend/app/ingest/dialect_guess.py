"""Dialect parameter guessing for a config no vendor profile recognises yet (design 5.5,
task D3). detect_dialect() (detector.py) picks the syntax family; this guesses the parser
knobs a draft profile needs, for the onboarding wizard (step 2, task I2) to show and let
the reviewer correct - it is never used to parse a config unconfirmed.
"""
from __future__ import annotations

from collections import Counter

# curated candidates only, so an ordinary hostname or ACL name is never mistaken for a
# block terminator (design: 'exit', 'quit', '}')
_TERMINATOR_CANDIDATES = {"end", "exit", "quit", "return", "}"}


def _body_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.strip()]


def guess_comment_prefix(text: str) -> str | None:
    """The most common single-character line, such as '!' or '#'."""
    counts = Counter(line.strip() for line in _body_lines(text) if len(line.strip()) == 1)
    return counts.most_common(1)[0][0] if counts else None


def guess_indent_unit(text: str) -> int | None:
    """The most common positive step between successive indentation depths."""
    steps, prev = Counter(), 0
    for line in _body_lines(text):
        depth = len(line) - len(line.lstrip(" "))
        if depth > prev:
            steps[depth - prev] += 1
        prev = depth
    return steps.most_common(1)[0][0] if steps else None


def guess_terminators(text: str, comment_prefix: str | None = None, limit: int = 3) -> list[str]:
    """Standalone lines from _TERMINATOR_CANDIDATES that recur, ranked by frequency."""
    lines = [line.strip() for line in _body_lines(text)]
    counts = Counter(line for line in lines
                     if line != comment_prefix and line.lower() in _TERMINATOR_CANDIDATES)
    return [word for word, n in counts.most_common(limit) if n >= 2]


def guess_negation_prefix(text: str) -> str | None:
    """The first token of lines whose remainder also appears on its own (design 5.5):
    e.g. 'undo telnet server enable' next to 'telnet server enable'."""
    lines = [line.strip() for line in _body_lines(text)]
    present = set(lines)
    counts = Counter()
    for line in lines:
        head, _, rest = line.partition(" ")
        if rest and rest in present:
            counts[head] += 1
    return counts.most_common(1)[0][0] if counts else None


def guess_dialect_params(text: str, dialect: str) -> dict:
    """Draft vendor_profile fields for an unrecognised config: '{dialect, ...knobs}'.
    Only guesses that were actually found are included, so an empty result still merges
    safely into the DialectHints defaults."""
    comment = guess_comment_prefix(text)
    out: dict = {"dialect": dialect}
    if comment:
        out["comment_prefixes"] = [comment]
    if dialect == "indent":
        indent_unit = guess_indent_unit(text)
        if indent_unit:
            out["indent_unit"] = indent_unit
    if dialect in ("indent", "brace"):
        terminators = guess_terminators(text, comment)
        if terminators:
            out["block_terminators"] = terminators
    negation = guess_negation_prefix(text)
    if negation:
        out["negation_prefixes"] = [negation]
    return out
