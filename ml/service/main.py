"""
ML service (design §12.8, task G1): FastAPI app exposing POST /suggest and POST /embed
over HTTP.  The backend calls these with a short timeout; on failure it queues templates
without suggestions (design §4.1, P3 "the pipeline never blocks on a human").

  uvicorn ml.service.main:app  [--host 0.0.0.0 --port 8001]

Environment:
  DATABASE_URL          same value as the backend (read-only: templates, keys, mappings)
  SIH_EMBEDDER          'st' (sentence-transformers, default) | 'hash' (offline)
  SIH_EMBED_MODEL       model name override (default: all-MiniLM-L6-v2)
  SIH_ML_MODEL_VERSION  version tag returned in every response (rollback: §12.9)
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

MODEL_VERSION = os.environ.get("SIH_ML_MODEL_VERSION", "heuristic-0")

_embedder = None  # loaded at startup


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _embedder
    from ml.service.embed import Embedder
    _embedder = Embedder()
    yield


app = FastAPI(title="SIH ML service", version="0.1.0", lifespan=lifespan)


# --------------------------------------------------------------------------- models
class SuggestRequest(BaseModel):
    template_id: int
    k: int = 3


class EmbedRequest(BaseModel):
    texts: list[str]


# --------------------------------------------------------------------------- helpers
def _db():
    from app.db.session import connect
    return connect()


def _confidence_decision(score: float) -> str:
    """Confidence bands from design §12.7 (whether to auto-approve is decided by the
    backend, which knows the per-vendor threshold setting; this just signals the band)."""
    if score >= 0.90:
        return "auto_approve"
    elif score >= 0.60:
        return "suggested"
    else:
        return "unknown"


# --------------------------------------------------------------------------- endpoints
@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "model_version": MODEL_VERSION,
            "embedder": getattr(_embedder, "name", None)}


@app.post("/embed")
def embed_texts(req: EmbedRequest) -> dict[str, Any]:
    """Encode texts to 384-d vectors (design §12.2, POST /embed)."""
    if not req.texts:
        return {"embeddings": [], "model": getattr(_embedder, "name", None)}
    vecs = _embedder.encode(req.texts)
    return {"embeddings": vecs, "model": _embedder.name}


@app.post("/suggest")
def suggest(req: SuggestRequest) -> dict[str, Any]:
    """
    Rank candidate canonical keys for an unknown template (design §12.8).

    Response:
      candidates    top-k dicts: key, score, knn, zero_shot, fit, proposal, preview, reasons
      decision      'auto_approve' | 'suggested' | 'unknown'  (design §12.7 bands)
      model_version version tag for rollback tracking (§12.9)
    """
    with _db() as conn:
        from app.registry import repository as registry
        from ml.service.rerank import Suggester

        tpl = registry.get_template(conn, str(req.template_id))
        if tpl is None:
            raise HTTPException(404, detail=f"template {req.template_id!r} not found")

        s = Suggester(conn, _embedder)
        s.ensure_embeddings([req.template_id])
        cands = s.candidates(tpl, req.k)

        top_score = cands[0]["score"] if cands else 0.0
        return {
            "candidates": cands,
            "decision": _confidence_decision(top_score),
            "model_version": MODEL_VERSION,
            "template_id": req.template_id,
        }
