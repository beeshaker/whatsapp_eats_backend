from __future__ import annotations

import os
import requests
from typing import Optional

# Base URL for your POS/Orders API
# Override in env with: API_BASE=http://localhost:8000
API_BASE  = os.getenv("API_BASE", "http://localhost:8000")
TENANT_ID = os.getenv("TENANT_ID", "1")
API_KEY   = os.getenv("API_KEY", "")


def _headers():
    """
    IMPORTANT: server expects 'X-Tenant-Id' (lowercase 'd'), not X-Tenant-ID.
    """
    h = {"X-Tenant-Id": str(TENANT_ID)}
    if API_KEY:
        h["Authorization"] = f"Bearer {API_KEY}"
    return h


def fetch_menu(restaurant_id: int | None = None):
    """
    Calls:
      GET /v1/public/menu[?restaurant_id=1]

    Returns the JSON payload from the API, e.g.:
      { "categories": [ { "name": "...",
                          "items": [ {id, name, price, desc, tags[]} ] } ] }
    """
    params = {}
    if restaurant_id is not None:
        params["restaurant_id"] = restaurant_id

    r = requests.get(
        f"{API_BASE}/v1/public/menu",
        headers=_headers(),
        params=params,
        timeout=10,
    )
    try:
        r.raise_for_status()
    except Exception:
        print("[MENU ERROR]", r.status_code, r.text, flush=True)
        raise
    return r.json()


def fetch_menu_pdf_urls(restaurant_id: int | None = None) -> list[str]:
    """
    Calls:
      GET /v1/public/menu_pdf[?restaurant_id=1]

    Expects:
      200: {"urls": ["https://.../v1/public/menu_pdf/main?restaurant_id=1", ...]}
      404: no PDFs configured → returns []

    Returns a simple list of URLs.
    """
    params = {}
    if restaurant_id is not None:
        params["restaurant_id"] = restaurant_id

    r = requests.get(
        f"{API_BASE}/v1/public/menu_pdf",
        headers=_headers(),
        params=params,
        timeout=8,
    )
    if r.status_code == 404:
        return []
    r.raise_for_status()
    data = r.json() or {}
    return [u for u in (data.get("urls") or []) if isinstance(u, str) and u]


def _fmt_price(v) -> str:
    try:
        f = float(v or 0)
        return str(int(f)) if f.is_integer() else f"{f}"
    except Exception:
        return "0"


def build_wa_sections(menu_json):
    """
    Convert /v1/public/menu JSON into WhatsApp List sections.

    Input shape (from API):
      {"categories":[
          {"name":"Pizzas",
           "items":[{"id":1,"name":"Margherita","price":650,"desc":"...","tags":[]}, ...]
          }, ...
      ]}

    Output shape (for send_list):
      [
        {
          "title": "Pizzas",
          "rows": [
            {
              "id": "add_1",
              "title": "Margherita — KSh 650",
              "description": "..."
            },
            ...
          ]
        },
        ...
      ]
    """
    sections = []
    for cat in menu_json.get("categories", []):
        rows = []
        for it in cat.get("items", []):
            rows.append({
                # Button/list row id; handled by ITEM_RE ^add_(\d+)$ in routes
                "id": f"add_{it['id']}",
                "title": f"{it['name']} — KSh {_fmt_price(it.get('price', 0))}",
                "description": (it.get("desc") or "")[:70],
            })
        if rows:
            sections.append({
                "title": cat.get("name", "Menu")[:24],
                "rows": rows[:10],   # WA limit: 10 rows per section
            })

    # WA limit: 10 sections max
    return sections[:10]
