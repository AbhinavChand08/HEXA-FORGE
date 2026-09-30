"""Text for embeddings, and the embedder (MiniLM, or an offline hashing fallback)."""
from __future__ import annotations

import os
import re

DIM = 384
TYPE_WORDS = {"NUM": "number", "IPV4": "ip address", "IPV6": "ipv6 address",
              "CIDR": "network prefix", "MASK": "netmask", "IFNAME": "interface",
              "SECRET": "secret", "QSTR": "text", "TEXT": "text", "WORD": "name",
              "ENUM": "option", "MAC": "mac address", "TIME": "time"}
PH = re.compile(r"^<([A-Z0-9]+)(\.\.\.)?>$")


def words(text: str) -> str:
    out = []
    for tok in text.split():
        m = PH.match(tok)
        if m:
            out.append(TYPE_WORDS.get(m.group(1), "value"))
            continue
        tok = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", tok)          # MinimumPasswordLength
        out.extend(w for w in re.split(r"[-_/.:\s]+", tok.lower()) if w)
    return " ".join(out)


def template_text(path: list[str], vendor: str | None = None,
                  slot_types: list[str] | None = None) -> str:
    """Rich text for embedding (design §12.2):
    'vendor junos | path services ssh | command connection limit | slots number'
    Including vendor and slot types gives the model context beyond just the path words."""
    leaf = words(path[-1])
    ctx = ", ".join(words(p) for p in reversed(path[:-1]))
    parts = []
    if vendor and vendor not in ("generic", ""):
        parts.append("vendor " + words(vendor.replace("-", " ").replace("_", " ")))
    if ctx:
        parts.append("path " + ctx)
    parts.append(leaf)
    if slot_types:
        type_str = " ".join(TYPE_WORDS.get(t, t.lower()) for t in slot_types)
        parts.append("slots " + type_str)
    return " | ".join(parts) if len(parts) > 1 else leaf


def key_text(k: dict) -> str:
    name = words(k["key"].replace(".", " ").replace("_", " "))
    aliases = ", ".join(k.get("aliases") or [])
    return f"{name}: {k['description']}" + (f". also called {aliases}" if aliases else "")


class Embedder:
    """sentence-transformers MiniLM (384-d); SIH_EMBEDDER=hash for an offline fallback.
    Every vector in one registry must come from the same embedder."""

    def __init__(self, kind: str | None = None):
        self.kind = kind or os.environ.get("SIH_EMBEDDER", "st")
        if self.kind == "st":
            os.environ.setdefault("USE_TF", "0")           # transformers would import TensorFlow
            os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
            from sentence_transformers import SentenceTransformer
            self.name = os.environ.get("SIH_EMBED_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
            self._model = SentenceTransformer(self.name)
            if self._model.get_sentence_embedding_dimension() != DIM:
                raise ValueError(f"{self.name} is not {DIM}-dimensional (schema uses vector({DIM}))")
        elif self.kind == "hash":
            from sklearn.feature_extraction.text import HashingVectorizer
            self.name = "hashing-char-3-5"
            self._model = HashingVectorizer(analyzer="char_wb", ngram_range=(3, 5),
                                            n_features=DIM, alternate_sign=False, norm="l2")
        else:
            raise ValueError(f"unknown embedder {self.kind!r} (st | hash)")

    def encode(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if self.kind == "st":
            return self._model.encode(texts, normalize_embeddings=True, batch_size=64).tolist()
        return self._model.transform(texts).toarray().tolist()
