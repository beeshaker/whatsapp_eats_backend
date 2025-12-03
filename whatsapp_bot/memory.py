# whatsapp_bot/memory.py
import time
from collections import defaultdict
from typing import Dict, Any, List, Optional, Tuple

# In production use Redis/DB; here is a process memory dict
# Add "addresses" list to your profile shape; keep your existing keys.
PROFILE: Dict[str, Dict[str, Any]] = defaultdict(
    lambda: {"prefs": {}, "dietary": [], "last_order": [], "addresses": [], "ts": 0}
)

def _now() -> int:
    return int(time.time())

def get_profile(wa_id: str) -> Dict[str, Any]:
    """Return the user profile dict (creates if missing)."""
    return PROFILE[wa_id]

def update_last_order(wa_id: str, items: List[Dict[str, Any]]):
    """Store last ordered items and bump timestamp."""
    p = PROFILE[wa_id]
    p["last_order"] = items or []
    p["ts"] = _now()

def set_pref(wa_id: str, key: str, value: Any):
    PROFILE[wa_id]["prefs"][key] = value

# -------------------------
# Addresses (NEW)
# -------------------------
def _addr_key(addr: Dict[str, Any]) -> Tuple[Optional[float], Optional[float], str]:
    """
    Build a dedup key. Prefer (lat,lng) if present; else normalized text.
    """
    lat = addr.get("lat")
    lng = addr.get("lng")
    if lat is not None and lng is not None:
        try:
            return (round(float(lat), 6), round(float(lng), 6), "")
        except Exception:
            pass
    text = (addr.get("text") or addr.get("label") or "").strip().lower()
    return (None, None, text)

def upsert_address(wa_id: str, addr: Dict[str, Any]) -> None:
    """
    Save or update a delivery address for the user.
    addr example:
      {
        "label": "Home",                  # optional
        "text": "Westlands, The Oval 6F",# optional (free-form)
        "lat": -1.268, "lng": 36.812      # optional (from WhatsApp location msg)
      }
    Dedups by (lat,lng) if both present; otherwise by normalized text.
    Increments 'used' and refreshes 'ts' on update.
    """
    p = PROFILE[wa_id]
    lst: List[Dict[str, Any]] = p.setdefault("addresses", [])

    key = _addr_key(addr)
    found = None
    for a in lst:
        if _addr_key(a) == key:
            found = a
            break

    if found:
        # update fields & bump counters
        for k, v in addr.items():
            if v not in (None, "", "null"):
                found[k] = v
        found["used"] = int(found.get("used", 0)) + 1
        found["ts"] = _now()
    else:
        a = dict(addr)
        a.setdefault("used", 1)
        a["ts"] = _now()
        lst.append(a)

def list_top_addresses(wa_id: str, limit: int = 3) -> List[Dict[str, Any]]:
    """
    Return addresses ordered by most-used then most-recent.
    """
    lst = PROFILE[wa_id].get("addresses") or []
    lst_sorted = sorted(
        lst,
        key=lambda a: (int(a.get("used", 0)) * -1, int(a.get("ts", 0)) * -1)
    )
    return lst_sorted[:max(0, limit)]
