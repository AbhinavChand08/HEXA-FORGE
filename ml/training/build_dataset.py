"""
build_dataset.py  -  turn approved mapping decisions into (features, label) training pairs
                     for the LightGBM reranker (design §12.4, task G4).

Usage:
  python -m ml.training.build_dataset --out /tmp/reranker_dataset.json [--min-labels 10]

Output: JSON Lines, one object per (template, candidate_key) pair:
  {
    "template_id": 1234,
    "key": "SSH.VERSION",
    "label": 1,          # 1 = chosen, 0 = rejected candidate
    "features": {
      "knn_max": 0.82,
      "votes": 0.45,
      "zero_shot": 0.71,
      "type_fit": 1.0,
      "scope_fit": 1.0,
      "synonym_overlap": 0.33,
      "category_prior": 0.5,
      "same_vendor_family": 1,
      "candidate_rank": 0    # 0 = top candidate
    }
  }
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from app.db.session import connect
from app.registry import repository as registry
from ml.service.embed import Embedder
from ml.service.features import category_prior, fit, lexical, same_vendor_family
from ml.service import retrieve


def _feature_row(tpl: dict, key: dict, rank: int, sib_cats: dict,
                 knn_max: float, best_nb, votes: float, zero_shot: float) -> dict:
    f, _ = fit(tpl, key)
    own_set = {v["type"] for v in tpl["vars"] if v["level"] == len(tpl["path"]) - 1}
    scope_ok = (not any(v["level"] < len(tpl["path"]) - 1 for v in tpl["vars"])
                if key["scope"] == "GLOBAL"
                else (any(v["level"] < len(tpl["path"]) - 1 for v in tpl["vars"])
                      or bool(own_set)))
    syn = lexical(tpl, key)
    cat_p = category_prior(sib_cats, key)
    svf = 1 if (best_nb and same_vendor_family(tpl, best_nb)) else 0
    return {
        "knn_max": round(knn_max, 4),
        "votes": round(votes, 4),
        "zero_shot": round(zero_shot, 4),
        "type_fit": round(f, 4),
        "scope_fit": 1.0 if scope_ok else 0.7,
        "synonym_overlap": round(syn, 4),
        "category_prior": round(cat_p, 4),
        "same_vendor_family": svf,
        "candidate_rank": rank,
    }


def build(conn, embedder: Embedder, min_labels: int = 1) -> list[dict]:
    """Query training_label rows and compute features for each (template, candidate) pair."""
    labels = conn.execute(
        """SELECT tl.template_id, tl.suggested_key, tl.chosen_key, tl.decision,
                  tl.candidates, tl.model_version
           FROM training_label tl
           ORDER BY tl.created_at""").fetchall()

    if len(labels) < min_labels:
        print(f"only {len(labels)} labels (need {min_labels}); skipping", file=sys.stderr)
        return []

    keys = registry.keys(conn)
    rows: list[dict] = []

    for lbl in labels:
        tpl = registry.get_template(conn, str(lbl["template_id"]))
        if tpl is None:
            continue
        # Ensure embedding exists
        retrieve.ensure_embeddings(conn, embedder, [tpl["id"]])

        knn_max, knn_votes, _, _ = retrieve.neighbour_votes(conn, tpl)
        zero_shot = retrieve.key_matches(conn, tpl, keys)
        sib_cats = retrieve.sibling_categories(conn, tpl)

        # candidates list from the label row (fallback: empty)
        cands = lbl["candidates"] or []
        chosen = lbl["chosen_key"]

        for rank, cand in enumerate(cands):
            cand_key = cand.get("key")
            if not cand_key or cand.get("kind") != "key":
                continue
            key = keys.get(cand_key)
            if key is None:
                continue
            ks, nb = knn_max.get(cand_key, (0.0, None))
            votes = knn_votes.get(cand_key, 0.0)
            zs = zero_shot.get(cand_key, 0.0)
            label = 1 if cand_key == chosen else 0
            rows.append({
                "template_id": lbl["template_id"],
                "key": cand_key,
                "label": label,
                "features": _feature_row(tpl, key, rank, sib_cats, ks, nb, votes, zs),
            })

    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Build reranker training dataset")
    parser.add_argument("--out", required=True, help="output path (.jsonl)")
    parser.add_argument("--min-labels", type=int, default=10,
                        help="abort if fewer labels exist")
    args = parser.parse_args()

    with connect() as conn:
        embedder = Embedder()
        rows = build(conn, embedder, args.min_labels)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    print(f"wrote {len(rows)} (template, key) pairs to {out}")


if __name__ == "__main__":
    main()
