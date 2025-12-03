# whatsapp_bot/orders.py
import os
import requests

API_BASE      = os.getenv("API_BASE", "http://localhost:8000")
TENANT_ID     = os.getenv("TENANT_ID", "1")
API_KEY       = os.getenv("API_KEY", "")
RESTAURANT_ID = int(os.getenv("RESTAURANT_ID", "1"))  # ← default per-bot

def _headers():
    # IMPORTANT: backend expects X-Tenant-Id (lowercase d)
    h = {"X-Tenant-Id": str(TENANT_ID)}
    if API_KEY:
        h["Authorization"] = f"Bearer {API_KEY}"
    return h


def checkout(
    user_id: str,
    name: str,
    phone: str,
    method: str = "pickup",
    address: str | None = None,
    restaurant_id: int | None = None,          # ← NEW PARAM
):
    """
    Create an order via /v1/orders.
    restaurant_id:
      - if provided explicitly, use that
      - otherwise default to RESTAURANT_ID from env
    """
    rid = restaurant_id or RESTAURANT_ID

    payload = {
        "user_id": user_id,
        "customer_name": name,
        "phone": phone,
        "restaurant_id": rid,                  # ← CRITICAL
        "fulfillment": {
            "type": method,
            "address": address,
        },
    }

    r = requests.post(
        f"{API_BASE}/v1/orders",
        json=payload,
        headers=_headers(),
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def mpesa_stk(order_id: str, phone: str):
    r = requests.post(
        f"{API_BASE}/v1/pay/mpesa/stk",
        json={"order_id": order_id, "msisdn": phone},
        headers=_headers(),
        timeout=15
    )
    r.raise_for_status()
    return r.json()


def fetch_order(order_code_or_id: str):
    r = requests.get(
        f"{API_BASE}/v1/orders/{order_code_or_id}",
        headers=_headers(),
        timeout=10
    )
    r.raise_for_status()
    return r.json()
