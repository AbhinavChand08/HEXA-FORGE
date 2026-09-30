"""ML suggestion engine (design section 12): retrieval-first mapping suggestions.

  embed.py     text representation and the embedder
  retrieve.py  candidate generation: kNN over reviewed templates, key descriptions
  features.py  re-rank signals: lexical match, keyword agreement, type / scope fit, polarity
  rerank.py    scoring, transform proposals, and the Suggester

Runs in-process for now; the HTTP service (main.py, POST /suggest) is task G1.
"""
