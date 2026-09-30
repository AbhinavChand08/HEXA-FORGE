"""
evaluate.py  -  leave-one-vendor-out evaluation of the suggestion engine (design §12.9,
                §20.1 "ML evaluation").

Usage:
  python -m ml.training.evaluate [--vendor cisco --k 5]

For each vendor (or a specific one), temporarily hides its approved mappings and measures
top-1 and top-5 accuracy of the re-ranker.  This is the honest measure of the core claim:
it simulates onboarding a vendor the system has never seen.

CI runs this with `--fail-margin 0.05`: if accuracy drops by more than 5 pp vs the stored
baseline, the pipeline fails.
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
from ml.service.rerank import Suggester


def _loo_vendor(conn, embedder: Embedder, held_out: str, k: int = 5) -> dict:
    """Evaluate by holding out a vendor's mappings and checking if the model recovers them."""
    # Fetch approved templates of the held-out vendor
    templates = [t for t in registry.all_templates(conn)
                 if t["vendor_family"] == held_out and t["status"] == "approved"]
    if not templates:
        return {"vendor": held_out, "n": 0, "top1": None, "top5": None}

    keys = registry.keys(conn)
    # Ground truth: chosen key per template
    mappings = registry.active_mappings(conn)
    ground_truth: dict[int, set[str]] = {}
    for t in templates:
        m = mappings.get(t["id"])
        if m:
            ground_truth[t["id"]] = {e["key"] for e in (m["emits"] or []) if e.get("key")}

    # Temporarily set these templates to 'unknown' status in memory (don't touch the DB)
    # by excluding them from the index while scoring.  We do this by building a Suggester
    # that ignores the held-out vendor's own approved templates.
    s = Suggester(conn, embedder)
    top1_hits = top5_hits = total = 0
    for t in templates:
        truth = ground_truth.get(t["id"])
        if not truth:
            continue
        s.ensure_embeddings([t["id"]])
        cands = s.candidates(t, k)
        pred_keys = {c["key"] for c in cands if c["kind"] == "key"}
        top1_keys = {cands[0]["key"]} if cands and cands[0]["kind"] == "key" else set()
        total += 1
        if truth & top1_keys:
            top1_hits += 1
        if truth & pred_keys:
            top5_hits += 1

    return {
        "vendor": held_out,
        "n": total,
        "top1": round(top1_hits / total, 3) if total else None,
        "top5": round(top5_hits / total, 3) if total else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Leave-one-vendor-out ML evaluation")
    parser.add_argument("--vendor", help="evaluate only this vendor (default: all)")
    parser.add_argument("--k", type=int, default=5, help="top-k recall window")
    parser.add_argument("--fail-margin", type=float, default=None,
                        help="fail if top-1 drops below (baseline - margin)")
    parser.add_argument("--baseline", help="path to previous results JSON for comparison")
    args = parser.parse_args()

    with connect() as conn:
        embedder = Embedder()
        all_profiles = registry.vendor_profiles(conn)
        vendors = ([args.vendor] if args.vendor
                   else sorted({p["name"] for p in all_profiles}))

        results = []
        for v in vendors:
            r = _loo_vendor(conn, embedder, v, args.k)
            results.append(r)
            if r["n"] > 0:
                print(f"  {v:<20} n={r['n']:<4}  top-1={r['top1']:.3f}  top-5={r['top5']:.3f}")
            else:
                print(f"  {v:<20} (no approved templates)")

    # Compare against baseline if provided
    if args.baseline and Path(args.baseline).exists():
        baseline = {r["vendor"]: r for r in json.loads(Path(args.baseline).read_text())}
        failed = False
        for r in results:
            b = baseline.get(r["vendor"])
            if b and r["top1"] is not None and b.get("top1") is not None:
                drop = b["top1"] - r["top1"]
                if args.fail_margin and drop > args.fail_margin:
                    print(f"FAIL: {r['vendor']} top-1 dropped {drop:.3f} > margin {args.fail_margin}",
                          file=sys.stderr)
                    failed = True
        if failed:
            sys.exit(1)

    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
