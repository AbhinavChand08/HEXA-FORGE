"""Domain types shared by parsing, templating and mapping."""
from __future__ import annotations

from dataclasses import dataclass, field

SECRET = "<SECRET>"
MAX_EXAMPLES = 5


def pick_examples(examples, limit: int = MAX_EXAMPLES) -> list[dict]:
    """Up to `limit` template examples spread over devices: the first example of every
    device, then the second of every device, ... Earlier examples win; duplicates dropped.
    An example's device is its 'device' field, else its source 'file'."""
    by_device: dict[str, list[dict]] = {}
    seen = set()
    for ex in examples:
        ident = (ex.get("device") or ex.get("file"), ex.get("line"), ex.get("raw"))
        if ident not in seen:
            seen.add(ident)
            by_device.setdefault(str(ident[0]), []).append(ex)
    out, rank = [], 0
    while len(out) < limit and any(rank < len(v) for v in by_device.values()):
        out += [v[rank] for v in by_device.values() if rank < len(v)][:limit - len(out)]
        rank += 1
    return out


@dataclass(eq=False)
class FlatCommand:
    """One flat record: a command with its parent path, polarity and source line."""
    tokens: list[str]
    src_line: int | None = None
    parent: "FlatCommand | None" = field(default=None, repr=False)
    is_block: bool = False
    raw: str = ""
    roles: list[str] | None = None     # structured formats: key / value / index / name
    quoted: list[bool] | None = None   # CLI formats: token was "quoted"
    negated: bool = False              # a negation prefix was stripped from tokens
    device_id: str | None = None
    id: int = -1
    parent_id: int | None = None
    context: list[list[str]] = field(default_factory=list)
    src_lines: list[int] | None = None  # every line a multi-line blob spans (None: just src_line)
    blob: str | None = None            # banner / certificate body, kept out of tokens
    secret_masked: bool = False        # a token was replaced by SECRET
    inactive: bool = False             # Junos 'inactive:' / 'protect:' marker (still parsed)

    @property
    def path(self) -> list[str]:
        return [" ".join(c) for c in self.context] + [" ".join(self.tokens)]

    @property
    def path_str(self) -> str:
        return " > ".join(self.path)

    @property
    def depth(self) -> int:
        return len(self.context)

    def to_dict(self) -> dict:
        return {"device_id": self.device_id, "path": self.path, "negated": self.negated,
                "src_line": self.src_line, "src_lines": self.src_lines, "id": self.id,
                "parent_id": self.parent_id, "context": self.context, "tokens": self.tokens,
                "roles": self.roles, "quoted": self.quoted, "is_block": self.is_block,
                "raw": self.raw, "blob": self.blob, "secret_masked": self.secret_masked,
                "inactive": self.inactive}
