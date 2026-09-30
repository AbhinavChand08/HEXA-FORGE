"""
train_reranker.py  -  train a LightGBM (or sklearn fallback) ranker on reviewer decisions
                      (design §12.4, task G4).

Usage:
  python -m ml.training.train_reranker --dataset /tmp/reranker_dataset.json \
                                       --out /tmp/reranker.model

Requirements:
  lightgbm   (preferred); falls back to sklearn RandomForestClassifier if absent
  scikit-learn

Run build_dataset.py first to produce the dataset file.

The trained model is a file that can be loaded at serving time by a future
`rerank_trained.py` to replace the hand-tuned weighted sum in rerank.py.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

FEATURE_COLS = [
    "knn_max", "votes", "zero_shot", "type_fit", "scope_fit",
    "synonym_overlap", "category_prior", "same_vendor_family", "candidate_rank",
]


def _load(dataset_path: str) -> tuple[list, list]:
    X, y = [], []
    with open(dataset_path) as f:
        for line in f:
            r = json.loads(line)
            feat = r["features"]
            X.append([feat.get(c, 0.0) for c in FEATURE_COLS])
            y.append(r["label"])
    return X, y


def _evaluate(model, X_test, y_test, name: str) -> None:
    preds = model.predict(X_test)
    tp = sum(p == l == 1 for p, l in zip(preds, y_test))
    fp = sum(p == 1 and l == 0 for p, l in zip(preds, y_test))
    fn = sum(p == 0 and l == 1 for p, l in zip(preds, y_test))
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    acc = sum(p == l for p, l in zip(preds, y_test)) / len(y_test) if y_test else 0.0
    print(f"[{name}] acc={acc:.3f}  prec={prec:.3f}  rec={rec:.3f}  f1={f1:.3f}")


def train_lightgbm(X_train, y_train, X_val, y_val):
    import lightgbm as lgb
    dtrain = lgb.Dataset(X_train, label=y_train, feature_name=FEATURE_COLS)
    dval = lgb.Dataset(X_val, label=y_val, reference=dtrain)
    params = {
        "objective": "binary",
        "metric": "binary_logloss",
        "learning_rate": 0.05,
        "num_leaves": 15,
        "min_data_in_leaf": 5,
        "verbose": -1,
    }
    model = lgb.train(params, dtrain, num_boost_round=200,
                      valid_sets=[dval],
                      callbacks=[lgb.early_stopping(20, verbose=False)])
    _evaluate_lgb(model, X_val, y_val)
    return model


def _evaluate_lgb(model, X_val, y_val) -> None:
    preds = [1 if p >= 0.5 else 0 for p in model.predict(X_val)]
    tp = sum(p == l == 1 for p, l in zip(preds, y_val))
    fp = sum(p == 1 and l == 0 for p, l in zip(preds, y_val))
    fn = sum(p == 0 and l == 1 for p, l in zip(preds, y_val))
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    acc = sum(p == l for p, l in zip(preds, y_val)) / len(y_val) if y_val else 0.0
    print(f"[lightgbm] acc={acc:.3f}  prec={prec:.3f}  rec={rec:.3f}  f1={f1:.3f}")


def train_sklearn(X_train, y_train, X_val, y_val):
    from sklearn.ensemble import GradientBoostingClassifier
    model = GradientBoostingClassifier(n_estimators=100, max_depth=3, learning_rate=0.1,
                                       random_state=42)
    model.fit(X_train, y_train)
    _evaluate(model, X_val, y_val, "sklearn-gbm")
    return model


def main() -> None:
    parser = argparse.ArgumentParser(description="Train reranker model")
    parser.add_argument("--dataset", required=True, help="path to .jsonl from build_dataset.py")
    parser.add_argument("--out", required=True, help="output path for the saved model")
    parser.add_argument("--val-frac", type=float, default=0.2, help="validation fraction")
    parser.add_argument("--min-samples", type=int, default=50,
                        help="minimum (template, key) pairs to attempt training")
    args = parser.parse_args()

    X, y = _load(args.dataset)
    if len(X) < args.min_samples:
        print(f"only {len(X)} samples (need {args.min_samples}); skip training", file=sys.stderr)
        sys.exit(0)

    # Stratified split
    import random
    random.seed(42)
    indices = list(range(len(X)))
    random.shuffle(indices)
    split = max(1, int(len(X) * (1 - args.val_frac)))
    train_idx, val_idx = indices[:split], indices[split:]
    X_train = [X[i] for i in train_idx]
    y_train = [y[i] for i in train_idx]
    X_val = [X[i] for i in val_idx]
    y_val = [y[i] for i in val_idx]

    print(f"training on {len(X_train)} pairs, validating on {len(X_val)}")
    print(f"class balance: {sum(y_train)} positive / {len(y_train)} total")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    try:
        model = train_lightgbm(X_train, y_train, X_val, y_val)
        model.save_model(str(out))
        print(f"saved lightgbm model to {out}")
    except ImportError:
        print("lightgbm not installed — falling back to sklearn GBM", file=sys.stderr)
        import pickle
        model = train_sklearn(X_train, y_train, X_val, y_val)
        with out.open("wb") as f:
            pickle.dump(model, f)
        print(f"saved sklearn model to {out}")


if __name__ == "__main__":
    main()
