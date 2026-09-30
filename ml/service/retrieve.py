"""Candidate generation (design §12.3): two retrieval passes over the registry.

G3 additions: weighted vote share, vendor/slot-type-aware embeddings.
"""
from __future__ import annotations

from app.registry import repository as registry
from ml.service.embed import Embedder, template_text
from ml.service.features import agreement, lexical, scope_fits


def _slot_types(tpl) -> list[str]:
    """Unique slot types at the leaf level for the embedding text (§12.2)."""
    depth = len(tpl.get("path") or []) - 1
    seen: list[str] = []
    for v in tpl.get("vars") or []:
        t = v.get("type", "")
        if v.get("level") == depth and t and t not in seen:
            seen.append(t)
    return seen


def ensure_embeddings(conn, embedder: Embedder, ids=None) -> int:
    """Compute and store embeddings for templates that are missing them.
    The text now includes vendor and slot types (§12.2, G3)."""
    rows = registry.missing_embeddings(conn, ids)
    if not rows:
        return 0
    texts = [
        template_text(r["path"],
                      vendor=None,   # path alone for templates without a full row
                      slot_types=None)
        for r in rows
    ]
    for r, v in zip(rows, embedder.encode(texts)):
        registry.set_embedding(conn, r["id"], v)
    return len(rows)


def ensure_embeddings_rich(conn, embedder: Embedder, templates: list[dict]) -> int:
    """Like ensure_embeddings but uses the full template row for vendor + slot types."""
    missing = [t for t in templates if not t.get("has_embedding")]
    if not missing:
        return 0
    texts = [
        template_text(t["path"],
                      vendor=t.get("vendor_family"),
                      slot_types=_slot_types(t))
        for t in missing
    ]
    for t, v in zip(missing, embedder.encode(texts)):
        registry.set_embedding(conn, t["id"], v)
    return len(missing)


def neighbour_votes(conn, tpl) -> tuple[dict, dict, tuple, tuple]:
    """Pass 1: nearest reviewed templates (any vendor), similarity scaled by keyword
    agreement.

    Returns:
      knn_max     {key: (max_sim, best_neighbour)}   highest-sim neighbour per key
      knn_votes   {key: weighted_vote_share}         fraction of total sim mass for key (G3)
      best_ignored  (sim, neighbour) or (0, None)
      best_scope    (sim, neighbour) or (0, None)
    """
    knn_max: dict[str, tuple[float, dict]] = {}
    knn_sums: dict[str, float] = {}   # sum of adjusted similarities voting for this key
    total_weight = 0.0
    ignore = scope_only = (0.0, None)

    for n in registry.neighbours(conn, tpl["id"]):
        sim = n["sim"] * agreement(tpl, n)
        if n["status"] == "ignored":
            ignore = max(ignore, (sim, n), key=lambda x: x[0])
            continue
        if not n["emits"] and n["scope"] and scope_fits(tpl, n):
            block = 1.0 if bool(tpl.get("is_block")) == bool(n["is_block"]) else 0.7
            scope_only = max(scope_only, (n["sim"] * block, n), key=lambda x: x[0])
            continue
        if not n["emits"]:
            continue
        total_weight += sim
        for e in n["emits"]:
            key = e["key"]
            if key not in knn_max or sim > knn_max[key][0]:
                knn_max[key] = (sim, n)
            knn_sums[key] = knn_sums.get(key, 0.0) + sim

    knn_votes = (
        {k: s / total_weight for k, s in knn_sums.items()}
        if total_weight > 0 else {}
    )
    return knn_max, knn_votes, ignore, scope_only


def key_matches(conn, tpl, keys: dict) -> dict[str, float]:
    """Pass 2: similarity to key descriptions, or a lexical alias match if higher."""
    zero_shot = {r["key"]: r["sim"] for r in registry.key_similarity(conn, tpl["id"])}
    for name, key in keys.items():
        zero_shot[name] = max(zero_shot.get(name, 0.0), lexical(tpl, key))
    return zero_shot


def sibling_categories(conn, tpl) -> dict[str, int]:
    """Category counts of keys emitted by templates that share the same parent (G3 §12.4
    category_prior). Approved siblings under the same parent predict the likely category."""
    if tpl.get("parent_id") is None:
        return {}
    rows = conn.execute(
        """SELECT ck.category
           FROM template t
           JOIN mapping m ON m.template_id = t.id AND m.active AND m.emits != '[]'::jsonb
           JOIN LATERAL jsonb_array_elements(m.emits) AS e ON true
           JOIN canonical_key ck ON ck.key = e->>'key'
           WHERE t.parent_id = %s AND t.id <> %s""",
        (tpl["parent_id"], tpl["id"])).fetchall()
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["category"]] = counts.get(r["category"], 0) + 1
    return counts
