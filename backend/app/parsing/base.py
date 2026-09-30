"""Dialect parser interface, and the knobs (DialectHints) a vendor profile fills in."""
from __future__ import annotations

import json
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Protocol

from app.core.models import FlatCommand
from app.parsing.blobs import CERT_BLOB


@dataclass
class DialectHints:
    """Generic parsing knobs. Profile rows and --hints override these, never code."""
    comment_prefixes: list[str] = field(default_factory=lambda: ["!", "#", "//", ";", ":"])
    ignore_prefixes: list[str] = field(default_factory=lambda: [
        "Building configuration", "Current configuration"])
    terminators: list[str] = field(default_factory=lambda: [
        "end", "exit", "exit all", "next", "quit", "return", "exit-address-family"])
    auto_detect_terminators: bool = True
    section_prefixes: list[str] = field(default_factory=lambda: ["/"])
    # 'banner motd ^C ... ^C'   (\x03 is the real control character)
    literal_delimiters: list[str] = field(default_factory=lambda: [
        "^C", "^c", "\x03", "^", "#", "%", "@", "~"])
    # blocks closed by a marker line: 'banner login' ... 'EOF' (Arista).
    # mask: body -> <SECRET>.  indented: body must be deeper than the start line
    literal_blocks: list[dict] = field(default_factory=lambda: [
        {"start": "banner", "end": "EOF"}, dict(CERT_BLOB)])
    # PEM keys / certificates anywhere in the file (also inside quotes, XML, JSON)
    blob_markers: list[dict] = field(default_factory=lambda: [
        {"begin": "-----BEGIN ", "end": "-----END ", "close": "-----"}])
    # stripped from the front of CLI statements; a negation sets negated=True
    negation_prefixes: list[str] = field(default_factory=lambda: ["no", "undo"])
    affirm_prefixes: list[str] = field(default_factory=list)
    block_openers: list[str] = field(default_factory=list)
    split_key_value: bool = True
    xml_root_as_context: bool = False
    # xml / json / yaml list items are keyed by the first of these fields found
    list_key_fields: list[str] = field(default_factory=lambda: ["name", "id"])
    hostname_keys: list[str] = field(default_factory=lambda: [
        "hostname", "host-name", "sysname"])
    max_literal_lines: int = 300

    # ---- secret redaction ----
    redact_secrets: bool = True
    secret_keywords: list[str] = field(default_factory=lambda: [
        "secret", "password", "passwd", "pwd", "key", "key-string",
        "authentication-key", "message-digest-key", "community", "psksecret",
        "pre-shared-key", "passphrase", "shared-secret", "auth-key", "auth-pwd",
        "priv-pwd"])
    # only redact after these if an algorithm word came first ('auth sha X')
    secret_keywords_after_algo: list[str] = field(default_factory=lambda: ["auth", "priv"])
    secret_skip_words: list[str] = field(default_factory=lambda: [
        "md5", "sha", "sha1", "sha224", "sha256", "sha384", "sha512", "aes",
        "aes128", "aes192", "aes256", "aes-128", "aes-192", "aes-256", "des",
        "3des", "hmac-md5", "hmac-sha1", "hmac-sha-256", "enc", "encrypted",
        "cipher", "irreversible-cipher", "scrypt", "plain", "clear"])
    secret_keep_values: list[str] = field(default_factory=lambda: ["public", "private"])
    secret_ignore_suffixes: list[str] = field(default_factory=lambda: [
        ".key", ".crt", ".pem", ".cer", ".p12", ".pfx"])
    # structured formats: redact values of keys containing these
    secret_key_patterns: list[str] = field(default_factory=lambda: [
        "password", "passwd", "secret", "passphrase", "psk", "presharedkey",
        "sharedkey", "authkey", "privkey", "community"])

    def update(self, data: dict) -> "DialectHints":
        known = {f.name for f in fields(self)}
        for key, value in data.items():
            if key in known:
                setattr(self, key, value)
        return self

    def overrides(self) -> dict:
        """Knobs that differ from the defaults (these win over the profile row)."""
        base = DialectHints()
        return {f.name: getattr(self, f.name) for f in fields(self)
                if getattr(self, f.name) != getattr(base, f.name)}

    @classmethod
    def load(cls, path: str | None) -> "DialectHints":
        hints = cls()
        if path:
            hints.update(json.loads(Path(path).read_text()))
        return hints


class DialectParser(Protocol):
    """One parser per dialect (parsing/dialects/); configured only through hints."""

    def __call__(self, text: str, hints: DialectHints) -> list[FlatCommand]: ...


# knobs a dialect needs when no vendor profile matched
DIALECT_DEFAULTS: dict[str, dict] = {
    "set": {"affirm_prefixes": ["set"], "negation_prefixes": ["delete", "unset"]},
}

# design section 6.2 parameter names -> DialectHints fields (values keep the DialectHints
# shapes: blob_starts entries are {start, end, mask?, indented?}, secret_patterns are keywords)
PARAM_ALIASES = {
    "block_terminators": "terminators",
    "blob_starts": "literal_blocks",
    "blob_delimiters": "literal_delimiters",
    "secret_patterns": "secret_keywords",
}
INFO_PARAMS = {"indent_unit"}          # accepted; the indent parser measures indentation
HINT_FIELDS = frozenset(f.name for f in fields(DialectHints))


def hints_from_params(params: dict) -> dict:
    """Profile dialect_params (design or DialectHints names) -> DialectHints knobs.
    'by_dialect' holds per-dialect overrides and is read by effective_hints."""
    out = {}
    for name, value in (params or {}).items():
        if name == "by_dialect" or name in INFO_PARAMS:
            continue
        field_name = PARAM_ALIASES.get(name, name)
        if field_name not in HINT_FIELDS:
            raise ValueError(f"unknown dialect parameter {name!r}")
        out[field_name] = value
    return out


def effective_hints(dialect: str, profile: dict, user: DialectHints | None) -> DialectHints:
    """defaults < dialect defaults < profile dialect_params < its by_dialect[dialect]
    < user --hints"""
    params = profile.get("dialect_params") or {}
    hints = DialectHints().update(DIALECT_DEFAULTS.get(dialect, {}))
    hints.update(hints_from_params(params))
    hints.update(hints_from_params((params.get("by_dialect") or {}).get(dialect, {})))
    return hints.update(user.overrides()) if user else hints
