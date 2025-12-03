# semantic_search.py
# Robust semantic + fuzzy search for menu items.
# - Works out of the box with RapidFuzz
# - Optionally uses FAISS + OpenAI embeddings if installed/configured
# - Supports alias dictionary and simple filtering (dietary/spice/budget/allergens)

from __future__ import annotations
from typing import List, Dict, Any, Optional, Tuple
import os
import json

# ----------------------------
# Optional deps (safe imports)
# ----------------------------
_USE_FAISS = False
try:
    import faiss  # type: ignore
    _USE_FAISS = True
except Exception:
    _USE_FAISS = False

try:
    from openai import OpenAI  # type: ignore
    _OPENAI = True
except Exception:
    _OPENAI = False

from rapidfuzz import process, fuzz  # always required for fallback

# ----------------------------
# Config
# ----------------------------
EMBED_MODEL = os.getenv("EMBED_MODEL", "text-embedding-3-small")
ALIAS_PATH = os.getenv("ALIAS_DICT_PATH", "alias_dict.json")        # optional
FAISS_INDEX_PATH = os.getenv("FAISS_INDEX_PATH", "menu_fallback.index")  # optional
FAISS_LABELS_PATH = os.getenv("FAISS_LABELS_PATH", "labels.json")        # optional

# ----------------------------
# Utilities
# ----------------------------
def _load_alias_dict() -> Dict[str, str]:
    if os.path.exists(ALIAS_PATH):
        try:
            with open(ALIAS_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

_ALIAS = _load_alias_dict()

def normalize_query(q: str) -> str:
    q = (q or "").strip().lower()
    return _ALIAS.get(q, q)

def _tags(item: Dict[str, Any]) -> set:
    return set([str(t).lower() for t in (item.get("tags") or [])])

def _passes_filters(
    item: Dict[str, Any],
    dietary: Optional[List[str]] = None,
    spice_level: Optional[str] = None,
    budget_kes: Optional[float] = None,
    allergens_blocklist: Optional[List[str]] = None,
) -> bool:
    t = _tags(item)
    # dietary (e.g., ["vegetarian","halal","gluten_free"])
    if dietary:
        wants = set([d.lower() for d in dietary])
        if not wants.intersection(t):
            return False
    # spice (e.g., "mild|medium|hot")
    if spice_level and spice_level.lower() not in t:
        return False
    # budget
    if budget_kes is not None and item.get("price", 10**9) > budget_kes:
        return False
    # allergens
    if allergens_blocklist:
        allergens = set([a.lower() for a in (item.get("allergens") or [])])
        if set([a.lower() for a in allergens_blocklist]).intersection(allergens):
            return False
    return True

# ----------------------------
# Fuzzy (RapidFuzz) matching
# ----------------------------
def fuzzy_best_matches(
    query: str,
    items: List[Dict[str, Any]],
    limit: int = 3,
    min_score: int = 70,
) -> List[Dict[str, Any]]:
    """
    Simple and fast fuzzy match on item names.
    Returns: [{"match": item_dict, "score": int}, ...]
    """
    q = normalize_query(query)
    choices = {it["name"]: it for it in items if it.get("name")}
    if not choices or not q:
        return []

    results = process.extract(q, choices.keys(), scorer=fuzz.WRatio, limit=limit)
    out = []
    for name, score, _ in results:
        if score >= min_score:
            out.append({"match": choices[name], "score": int(score)})
    return out

# ----------------------------
# FAISS + OpenAI embeddings (optional)
# ----------------------------
_client = None
if _USE_FAISS and _OPENAI and os.getenv("OPENAI_API_KEY"):
    try:
        _client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    except Exception:
        _client = None

_faiss_index = None
_faiss_labels: List[Any] = []

def _load_faiss():
    global _faiss_index, _faiss_labels
    if not (_USE_FAISS and _client):
        return
    if os.path.exists(FAISS_INDEX_PATH) and os.path.exists(FAISS_LABELS_PATH):
        try:
            _faiss_index = faiss.read_index(FAISS_INDEX_PATH)
            with open(FAISS_LABELS_PATH, "r", encoding="utf-8") as f:
                _faiss_labels = json.load(f)
        except Exception:
            _faiss_index = None
            _faiss_labels = []

def _embed(texts: List[str]) -> List[List[float]]:
    if not _client:
        return []
    resp = _client.embeddings.create(model=EMBED_MODEL, input=texts)
    return [d.embedding for d in resp.data]

def faiss_best_matches(
    query: str,
    limit: int = 3,
) -> List[Tuple[Any, float]]:
    """
    Look up nearest neighbors in the FAISS index.
    Returns: [(label_obj, distance), ...]
    label_obj is whatever was stored alongside embeddings (e.g., your canonical item dict).
    """
    if not (_faiss_index and _client):
        return []
    q = normalize_query(query)
    if not q:
        return []
    vecs = _embed([q])
    if not vecs:
        return []
    import numpy as np  # local import
    v = np.array(vecs, dtype="float32")
    D, I = _faiss_index.search(v, limit)
    out = []
    for dist, idx in zip(D[0], I[0]):
        if idx == -1:
            continue
        out.append((_faiss_labels[idx], float(dist)))
    return out

# Load FAISS once if available
_load_faiss()

# ----------------------------
# Public API
# ----------------------------
def best_matches(
    query: str,
    items: List[Dict[str, Any]],
    limit: int = 3,
    min_score: int = 70,
) -> List[Dict[str, Any]]:
    """
    Hybrid matcher:
      1) If FAISS is available AND you have a trained index, try FAISS first.
      2) Fall back to fuzzy matching by name.
    Normalizes the query with alias dictionary first.
    """
    # Try FAISS (labels should be canonical items; trainer can store corrected pairs)
    faiss_hits = faiss_best_matches(query, limit=limit) if (_faiss_index and _client) else []
    results: List[Dict[str, Any]] = []

    # Convert FAISS hits into the same structure as fuzzy for consistency
    for label, dist in faiss_hits:
        # Lower distance = better; convert to a pseudo-score out of 100
        score = max(0, int(100 - (dist * 100)))  # heuristic
        # label can be an item dict or {"id":..., "name":..., "price":..., "tags":[...]}
        if isinstance(label, dict) and label.get("name"):
            results.append({"match": label, "score": score})

    # If FAISS insufficient, add fuzzy matches
    if len(results) < limit:
        fuzzy_needed = limit - len(results)
        fuzzy_res = fuzzy_best_matches(query, items, limit=fuzzy_needed, min_score=min_score)
        # Avoid duplicates by item id or name
        seen = set()
        for r in results:
            key = r["match"].get("id") or r["match"].get("name")
            seen.add(str(key))
        for r in fuzzy_res:
            key = r["match"].get("id") or r["match"].get("name")
            if str(key) not in seen:
                results.append(r)

    # Sort by score desc
    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:limit]

def suggest_items(
    items: List[Dict[str, Any]],
    dietary: Optional[List[str]] = None,
    spice_level: Optional[str] = None,
    budget_kes: Optional[float] = None,
    allergens_blocklist: Optional[List[str]] = None,
    sort_by: str = "price_asc",  # or "price_desc"
    limit: int = 10,
) -> List[Dict[str, Any]]:
    """
    Filter + rank items for recommendations (used by ASK_QUESTION intent).
    """
    pool = [
        it for it in items
        if _passes_filters(
            it,
            dietary=dietary,
            spice_level=spice_level,
            budget_kes=budget_kes,
            allergens_blocklist=allergens_blocklist,
        )
    ]
    if sort_by == "price_desc":
        pool.sort(key=lambda x: x.get("price", 0), reverse=True)
    else:
        pool.sort(key=lambda x: x.get("price", 0))
    return pool[:limit]

def format_top_matches(matches: List[Dict[str, Any]]) -> str:
    """
    Utility to turn matches into a human-friendly list (optional).
    """
    if not matches:
        return "No matching items found."
    lines = []
    for m in matches:
        it = m["match"]
        price = it.get("price", 0)
        lines.append(f"• {it.get('name')} — KSh {price} (score {m['score']})")
    return "\n".join(lines)
