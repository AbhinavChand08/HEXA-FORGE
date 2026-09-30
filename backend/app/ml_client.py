"""
ml_client.py  -  HTTP client for the ML suggestion service (design §12.8, task G1).

The backend calls this module after normalize() produces new unknown templates.
If ML_SERVICE_URL is empty or the request fails, the function returns None so the
normalizer can queue the template without suggestions (P3 "pipeline never blocks").

G2 confidence routing:
  decision == 'auto_approve' + vendor.auto_approve == True  ->  save mapping as active
                                                                  source='auto_ml'
  decision == 'suggested'                                   ->  save as inactive model mapping
  decision == 'unknown'                                     ->  no mapping stored
"""
from __future__ import annotations

import logging
import urllib.error
import urllib.parse
import urllib.request
import json as _json

from app import settings

log = logging.getLogger(__name__)

# Vendor profile key for the auto-approve setting (stored in dialect_params JSONB).
_AUTO_APPROVE_KEY = "auto_approve"


def _post(path: str, body: dict) -> dict | None:
    """POST JSON to the ML service.  Returns parsed JSON or None on any error."""
    url = settings.ML_SERVICE_URL.rstrip("/") + path
    data = _json.dumps(body).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=settings.ML_SERVICE_TIMEOUT) as resp:
            return _json.loads(resp.read())
    except urllib.error.URLError as exc:
        log.warning("ML service unreachable (%s): %s", url, exc)
    except Exception as exc:  # noqa: BLE001
        log.warning("ML service error (%s): %s", url, exc)
    return None


def suggest(template_id: int, k: int = 3) -> dict | None:
    """Call POST /suggest and return the response dict, or None if unavailable."""
    if not settings.ML_SERVICE_URL:
        return None
    return _post("/suggest", {"template_id": template_id, "k": k})


def embed(texts: list[str]) -> list[list[float]] | None:
    """Call POST /embed and return a list of vectors, or None if unavailable."""
    if not settings.ML_SERVICE_URL or not texts:
        return None
    resp = _post("/embed", {"texts": texts})
    return (resp or {}).get("embeddings")


# --------------------------------------------------------------------------- G2 routing
def apply_suggestion(conn, template_id: int, vendor_profile: dict | None,
                     k: int = 3) -> dict | None:
    """
    Fetch a suggestion from the ML service and optionally store it in the registry.

    Design §12.7 confidence routing:
      ≥0.90 + auto_approve enabled  ->  approved mapping (source = 'auto_ml')
      0.60–0.90                     ->  suggested mapping (source = 'model', inactive)
      <0.60                         ->  template stays unknown; candidates stored in evidence

    Returns the response dict from /suggest, or None when the service is unavailable.
    """
    resp = suggest(template_id, k)
    if resp is None:
        return None

    from app.registry import repository as registry

    candidates = resp.get("candidates") or []
    decision = resp.get("decision", "unknown")
    model_version = resp.get("model_version")

    if not candidates:
        return resp

    best = candidates[0]
    prop = (best.get("proposal") or {})
    emits = prop.get("emits") or []
    scope = prop.get("scope")
    slot_names = prop.get("slot_names") or []

    # Determine whether auto-approve is enabled for this vendor.
    auto_approve_enabled = bool(
        (vendor_profile or {}).get("dialect_params", {}).get(_AUTO_APPROVE_KEY, False)
    )
    should_auto_approve = (decision == "auto_approve" and auto_approve_enabled
                           and best["kind"] == "key")

    if should_auto_approve:
        # Auto-approve: write an active mapping (source = 'auto_ml').
        registry.save_mapping(
            conn, template_id, emits=emits, scope=scope, slot_names=slot_names,
            source="auto_ml", confidence=best["score"], active=True,
            evidence={"top": best["key"], "candidates": candidates,
                      "model_version": model_version, "auto_approved": True})
        log.info("auto-approved template %s -> %s (score %.2f)",
                 template_id, best["key"], best["score"])
    elif decision in ("auto_approve", "suggested"):
        # Suggested (or auto-approve disabled): store as an inactive model mapping.
        registry.save_mapping(
            conn, template_id, emits=emits, scope=scope, slot_names=slot_names,
            source="model", confidence=best["score"], active=False,
            evidence={"top": best["key"], "candidates": candidates,
                      "model_version": model_version})
    # decision == 'unknown': nothing stored; candidates returned in resp for the queue

    return resp
