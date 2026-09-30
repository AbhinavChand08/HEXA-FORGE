"""Secret redaction: passwords, hashes, keys and communities become <SECRET>."""
from __future__ import annotations

from app.core.models import SECRET, FlatCommand


def _plain_word(tok: str) -> bool:
    """Command-like word: lowercase letters and hyphens only."""
    return bool(tok) and all((c.isalpha() and c.islower()) or c == "-" for c in tok)


def looks_like_hash(tok: str) -> bool:
    if len(tok) >= 10 and tok.startswith("$") and tok.count("$") >= 2:
        return True                                           # $9$..., $6$...
    if (len(tok) >= 16 and all(c in "0123456789abcdefABCDEF" for c in tok)
            and any(c.isdigit() for c in tok) and any(c.isalpha() for c in tok)):
        return True                                           # long hex
    if (len(tok) >= 24 and not any(c in tok for c in " -_.:")
            and any(c.isupper() for c in tok) and any(c.islower() for c in tok)
            and any(c.isdigit() for c in tok) and not _name_number(tok)):
        return True                                           # base64 blobs
    return False


def _name_number(tok: str) -> bool:
    """HundredGigabitEthernet1/0/1 shape: letters, then only digits and / : ."""
    i = 0
    while i < len(tok) and tok[i].isalpha():
        i += 1
    return 0 < i < len(tok) and all(c.isdigit() or c in "/:." for c in tok[i:])


def _redact_cli(tokens: list[str], hints) -> list[str]:
    toks = list(tokens)
    kws = {k.lower() for k in hints.secret_keywords}
    algo_kws = {k.lower() for k in hints.secret_keywords_after_algo}
    skip = {k.lower() for k in hints.secret_skip_words}
    keep = {k.lower() for k in hints.secret_keep_values}
    for i, tok in enumerate(tokens):
        low = tok.lower()
        if low not in kws and low not in algo_kws:
            continue
        int_seen = algo_seen = False
        for j in range(i + 1, min(len(toks), i + 6)):
            cand = toks[j]
            cl = cand.lower()
            if cand == SECRET:
                break
            if cand.isdigit() and len(cand) <= 3:        # type / key-id numbers
                int_seen = True
                continue
            if cl in skip:                               # md5, sha512, ENC ...
                algo_seen = True
                continue
            if any(cl.endswith(s) for s in hints.secret_ignore_suffixes):
                break                                    # file names, not secrets
            if low in algo_kws and not algo_seen:
                break                                    # 'auth radius' is not a secret
            if (int_seen or algo_seen or not _plain_word(cand)) and cl not in keep:
                toks[j] = SECRET
            break
    return toks


_BENIGN = {"enable", "disable", "enabled", "disabled", "on", "off", "true", "false",
           "yes", "no", "none", "null", ""}


def _redact_structured(cmd: FlatCommand, hints) -> None:
    pats = [p.lower() for p in hints.secret_key_patterns]
    for i in range(1, len(cmd.tokens)):
        if cmd.roles[i] != "value" or cmd.roles[i - 1] != "key":
            continue
        key, val = cmd.tokens[i - 1].lower(), cmd.tokens[i]
        if any(p in key for p in pats) and val.lower() not in _BENIGN and not val.isdigit():
            cmd.tokens[i] = SECRET


def redact(commands: list[FlatCommand], hints) -> None:
    for cmd in commands:
        # a masked blob inside quotes can keep padding: '"<SECRET>   "'
        cmd.tokens = [SECRET if t.strip() == SECRET else t for t in cmd.tokens]
        before = list(cmd.tokens)
        if cmd.roles is not None:
            _redact_structured(cmd, hints)
        else:
            cmd.tokens = _redact_cli(cmd.tokens, hints)
        cmd.tokens = [SECRET if t != SECRET and looks_like_hash(t) else t
                      for t in cmd.tokens]
        if cmd.blob == SECRET:
            cmd.secret_masked = True
        for old, new in zip(before, cmd.tokens):
            if new == SECRET and old and old != SECRET:
                cmd.raw = cmd.raw.replace(old, SECRET)
                cmd.secret_masked = True
