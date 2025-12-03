import os
import requests
from typing import List, Dict, Optional, Any

API_BASE = os.getenv("API_BASE", "http://localhost:8000")
TENANT_ID = os.getenv("TENANT_ID", "1")
API_KEY = os.getenv("API_KEY", "")
DEFAULT_RESTAURANT_ID = int(os.getenv("DEFAULT_RESTAURANT_ID", "1"))


def _headers(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """
    Base headers for all API calls.
    """
    h: Dict[str, str] = {"X-Tenant-Id": TENANT_ID}  # <-- match server helper _tenant_id()
    if API_KEY:
        h["Authorization"] = f"Bearer {API_KEY}"
    if extra:
        h.update(extra)
    return h


# ---------------------------
# 🔍 Variants (optional)
# ---------------------------

def get_variants(item_id: int, restaurant_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """
    Returns a list of variants for an item. Each variant is a dict like:
      {"id": 12, "name": "Large", "price": 650, "is_available": True}

    NOTE: This assumes you expose:
      GET /v1/public/item/{item_id}/variants[?restaurant_id=1]

    If you don't have variants yet, you can either:
      - implement that endpoint on the API, OR
      - have this function just return [] and let the bot treat items as single-price.
    """
    params: Dict[str, Any] = {}
    if restaurant_id is not None:
        params["restaurant_id"] = restaurant_id

    url = f"{API_BASE}/v1/public/item/{int(item_id)}/variants"
    r = requests.get(url, params=params, headers=_headers(), timeout=10)
    r.raise_for_status()
    data = r.json() or {}
    variants = data.get("variants", data)  # allow either list or {"variants":[...]}

    norm: List[Dict[str, Any]] = []
    for v in variants or []:
        norm.append({
            "id": int(v.get("id") or v.get("variant_id")),
            "name": v.get("name") or v.get("label") or "Variant",
            "price": float(
                v.get("price")
                or v.get("unit_price")
                or v.get("amount")
                or 0
            ),
            "is_available": bool(v.get("is_available", True)),
        })

    # Only available ones (you can remove this filter if you want to show all)
    return [v for v in norm if v["is_available"]]


# ---------------------------
# 🧺 Public cart endpoints
# ---------------------------

def add_to_cart(
    user_id: str,
    item_id: int,
    qty: int = 1,
    restaurant_id: Optional[int] = None,
):
    """
    Simple add-to-cart used by the bot.
    Hits: POST /v1/public/cart/add
    """
    if restaurant_id is None:
        restaurant_id = DEFAULT_RESTAURANT_ID

    payload = {
        "user_id": user_id,
        "item_id": int(item_id),
        "qty": int(qty),
        "restaurant_id": restaurant_id,
    }
    r = requests.post(
        f"{API_BASE}/v1/public/cart/add",
        json=payload,
        headers=_headers(),
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


def get_cart(user_id: str, restaurant_id: Optional[int] = None):
    """
    GET current cart snapshot (server will create one if missing).
    Hits: GET /v1/public/cart
    """
    if restaurant_id is None:
        restaurant_id = DEFAULT_RESTAURANT_ID

    params = {
        "user_id": user_id,
        "restaurant_id": restaurant_id,
    }
    r = requests.get(
        f"{API_BASE}/v1/public/cart",
        params=params,
        headers=_headers(),
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


def clear_cart(user_id: str, restaurant_id: Optional[int] = None):
    """
    Clear current cart for a user.
    Hits: POST /v1/public/cart/clear
    """
    if restaurant_id is None:
        restaurant_id = DEFAULT_RESTAURANT_ID

    payload = {
        "user_id": user_id,
        "restaurant_id": restaurant_id,
    }
    r = requests.post(
        f"{API_BASE}/v1/public/cart/clear",
        json=payload,
        headers=_headers(),
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


# ---------------------------
# 🔧 UPDATE CART (bulk ops)
# ---------------------------

def update_cart(
    user_id: str,
    ops: List[Dict[str, Any]],
    restaurant_id: Optional[int] = None,
):
    """
    Low-level bulk operations (add/remove/set_qty etc.).
    Hits: POST /v1/cart/update  (internal, license-protected)
    """
    if restaurant_id is None:
        restaurant_id = DEFAULT_RESTAURANT_ID

    payload = {
        "wa_phone": user_id,          # <-- server expects this name
        "restaurant_id": restaurant_id,
        "ops": ops,
    }
    r = requests.post(
        f"{API_BASE}/v1/cart/update",
        json=payload,
        headers=_headers(),
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


# ---------------------------
# 🛠️ Convenience helpers
# ---------------------------

def set_qty(
    user_id: str,
    item_id: int,
    qty: int,
    variant_id: Optional[int] = None,
    restaurant_id: Optional[int] = None,
):
    """
    Set quantity for an item (qty=0 will effectively remove if supported server-side).
    """
    return update_cart(
        user_id,
        [{
            "op": "set_qty",
            "item_id": item_id,
            "variant_id": variant_id,
            "qty": qty,
        }],
        restaurant_id=restaurant_id,
    )


def remove_item(
    user_id: str,
    item_id: int,
    variant_id: Optional[int] = None,
    restaurant_id: Optional[int] = None,
):
    return update_cart(
        user_id,
        [{
            "op": "remove",
            "item_id": item_id,
            "variant_id": variant_id,
        }],
        restaurant_id=restaurant_id,
    )


def change_variant(
    user_id: str,
    item_id: int,
    old_variant_id: Optional[int],
    new_variant_id: Optional[int],
    restaurant_id: Optional[int] = None,
):
    """
    NOTE: Server-side `update_cart` currently only understands: add/remove/set_qty.
    Calling this will send `op="change_variant"` which is currently a NO-OP
    unless you extend the API to support it.
    """
    return update_cart(
        user_id,
        [{
            "op": "change_variant",
            "item_id": item_id,
            "old_variant_id": old_variant_id,
            "new_variant_id": new_variant_id,
        }],
        restaurant_id=restaurant_id,
    )


def set_note(
    user_id: str,
    item_id: int,
    note: str,
    variant_id: Optional[int] = None,
    restaurant_id: Optional[int] = None,
):
    """
    Same note as above: server ignores `set_note` unless implemented.
    """
    return update_cart(
        user_id,
        [{
            "op": "set_note",
            "item_id": item_id,
            "variant_id": variant_id,
            "note": note,
        }],
        restaurant_id=restaurant_id,
    )


def set_options(
    user_id: str,
    item_id: int,
    options: Dict[str, Any],
    variant_id: Optional[int] = None,
    restaurant_id: Optional[int] = None,
):
    """
    options is a free-form dict (e.g., toppings). Server may price options.
    Currently a NO-OP unless you extend the backend to handle `set_options`.
    """
    return update_cart(
        user_id,
        [{
            "op": "set_options",
            "item_id": item_id,
            "variant_id": variant_id,
            "options": options,
        }],
        restaurant_id=restaurant_id,
    )
