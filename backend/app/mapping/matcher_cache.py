"""Per-vendor trie cache with lazy hot-reload on registry version change (F2).

A job pins the registry version at start (Normalizer.reload pins self.registry_version).
The cache keeps the last-built trie per vendor family; if the registry version advanced,
the next call rebuilds only that vendor's trie rather than all vendors.

Thread-safety: each Normalizer instance has its own MatcherCache; the cache is not shared
across threads/processes. The design is deliberately simple: a process-restart flushes
the cache, which is the safe default.
"""
from __future__ import annotations

from app.mapping.compiler import build_index
from app.registry import repository as registry


class MatcherCache:
    """Lazy per-vendor trie.  Call get_index(conn, family) to obtain a current trie."""

    def __init__(self):
        # {vendor_family: (registry_version, trie_index)}
        self._cache: dict[str, tuple[int, dict]] = {}

    def get_index(self, conn, family: str, templates: list[dict] | None = None) -> dict:
        """Return the trie for *family*, rebuilding if stale.

        If *templates* is given (the caller already loaded all templates), they are used
        for the rebuild so we avoid a redundant DB round-trip.  Otherwise the cache
        fetches the family's own templates from the registry.
        """
        current_v = registry.current_version(conn)
        cached = self._cache.get(family)
        if cached is not None and cached[0] == current_v:
            return cached[1]

        # Rebuild: prefer the caller-supplied slice, else fetch from DB.
        if templates is not None:
            family_tpls = [t for t in templates if t["vendor_family"] == family]
        else:
            family_tpls = registry.templates_by_family(conn, family)
            # Normalise to the shape build_index expects (parent = parent_signature).
            for t in family_tpls:
                if "parent" not in t:
                    t["parent"] = t.get("parent_signature")

        trie = build_index(family_tpls)
        self._cache[family] = (current_v, trie)
        return trie

    def mark_stale(self, family: str) -> None:
        """Force a rebuild on the next get_index call for this vendor."""
        self._cache.pop(family, None)

    def invalidate_all(self) -> None:
        self._cache.clear()
