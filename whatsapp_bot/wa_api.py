# whatsapp_bot/wa_api.py
import os
import json
import time
from typing import Iterable, Union, Dict, List, Optional
import requests

# -------------------------------------------------------------------
# WhatsApp Cloud API config
# -------------------------------------------------------------------
WABA_TOKEN = os.getenv("WABA_ACCESS_TOKEN")
PHONE_ID   = os.getenv("WABA_PHONE_NUMBER_ID")

if not WABA_TOKEN or not PHONE_ID:
    raise RuntimeError("Missing env vars: WABA_ACCESS_TOKEN and/or WABA_PHONE_NUMBER_ID")

API_BASE     = "https://graph.facebook.com/v20.0"
MESSAGES_URL = f"{API_BASE}/{PHONE_ID}/messages"

# -------------------------------------------------------------------
# Backend logging config (for HTMX chat UI)
# -------------------------------------------------------------------
BACKEND_BASE = os.getenv("WHATSAPP_BACKEND_BASE", "http://localhost:8000").rstrip("/")
TENANT_ID    = os.getenv("TENANT_ID", "1")

def _headers() -> dict:
    return {
        "Authorization": f"Bearer {WABA_TOKEN}",
        "Content-Type": "application/json",
    }

def _post(payload: dict, *, timeout: int = 15) -> dict:
    """POST helper with good error logging."""
    try:
        r = requests.post(MESSAGES_URL, headers=_headers(), json=payload, timeout=timeout)
        if r.status_code >= 400:
            try:
                err = r.json()
            except Exception:
                err = r.text
            print(
                "[WABA ERROR]",
                r.status_code,
                json.dumps(err, ensure_ascii=False),
                "\nPayload:",
                json.dumps(payload, ensure_ascii=False, indent=2),
                flush=True,
            )
            r.raise_for_status()
        return r.json()
    except Exception as e:
        print("[WABA SEND FAILED]", e, flush=True)
        return {}

# -------------------------------------------------------------------
# Helper: extract WhatsApp message ID from response
# -------------------------------------------------------------------
def _extract_wa_msg_id(resp: dict) -> Optional[str]:
    try:
        msgs = resp.get("messages") or []
        if isinstance(msgs, list) and msgs:
            mid = msgs[0].get("id")
            if isinstance(mid, str) and mid.startswith("wamid."):
                return mid
    except Exception:
        pass
    return None

# -------------------------------------------------------------------
# Helper: log outbound messages to backend (so admin UI shows both sides)
# -------------------------------------------------------------------
def _log_outbound(
    wa_id: str,
    *,
    text: str = "",
    msg_type: str = "text",
    wa_msg_id: Optional[str] = None,
    media_url: Optional[str] = None,
    meta: Optional[dict] = None,
) -> None:
    """
    Fire-and-forget POST to /v1/whatsapp/log_outbound
    This makes bot messages appear in the admin chat view.
    """
    if not BACKEND_BASE:
        return

    payload = {
        "to": wa_id,
        "wa_msg_id": wa_msg_id or "",
        "type": msg_type,
        "text": text or "",
        "media_url": media_url or "",
        "timestamp": int(time.time()),
        "meta": meta or {},
    }

    try:
        r = requests.post(
            f"{BACKEND_BASE}/v1/whatsapp/log_outbound",
            headers={
                "Content-Type": "application/json",
                "X-Tenant-Id": str(TENANT_ID),
            },
            json=payload,
            timeout=5,
        )
        if r.status_code >= 400:
            print("[LOG_OUTBOUND FAILED]", r.status_code, r.text[:300], flush=True)
        else:
            print("[LOG_OUTBOUND OK]", wa_msg_id or "no-id", flush=True)
    except Exception as e:
        print("[LOG_OUTBOUND ERROR]", e, flush=True)


# -------------------------------------------------------------------
# Public senders — all now log to backend automatically
# -------------------------------------------------------------------
def send_text(wa_id: str, text: str) -> dict:
    """Send a plain text message."""
    payload = {
        "messaging_product": "whatsapp",
        "to": wa_id,
        "type": "text",
        "text": {"body": text[:4096]},
    }
    resp = _post(payload)
    wa_msg_id = _extract_wa_msg_id(resp)

    _log_outbound(
        wa_id,
        text=text[:4096],
        msg_type="text",
        wa_msg_id=wa_msg_id,
        meta={"source": "bot"},
    )
    return resp


def send_quick_replies(
    wa_id: str,
    body: str,
    buttons: Union[List[str], List[Dict[str, str]]],
) -> dict:
    """
    Send up to 3 quick reply buttons.
    buttons: list[str] or list[{"id": "...", "title": "..."}]
    """
    norm_buttons = []
    for btn in buttons[:3]:
        if isinstance(btn, dict):
            btn_id = str(btn.get("id", "btn"))
            title = str(btn.get("title", btn_id))
        else:
            btn_id = title = str(btn)

        norm_buttons.append({
            "type": "reply",
            "reply": {
                "id": btn_id[:256],
                "title": title[:20],
            },
        })

    payload = {
        "messaging_product": "whatsapp",
        "to": wa_id,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body[:1024]},
            "action": {"buttons": norm_buttons},
        },
    }
    resp = _post(payload)
    wa_msg_id = _extract_wa_msg_id(resp)

    _log_outbound(
        wa_id,
        text=body[:1024],
        msg_type="interactive",
        wa_msg_id=wa_msg_id,
        meta={"source": "bot", "kind": "quick_replies", "buttons": [b["reply"]["title"] for b in norm_buttons]},
    )
    return resp


def send_list(wa_id: str, body: str, sections: List[Dict]) -> dict:
    """
    Send an interactive list message.
    Respects WhatsApp limits: max 10 sections, 10 rows each.
    """
    trimmed_sections = []
    for sec in sections[:10]:
        rows = sec.get("rows", [])[:10]
        trimmed_rows = []
        for row in rows:
            trimmed_rows.append({
                "id": str(row.get("id", ""))[:200],
                "title": str(row.get("title", "Item"))[:24],
                **({"description": str(row.get("description", ""))[:72]} if row.get("description") else {}),
            })
        if trimmed_rows:
            trimmed_sections.append({
                "title": str(sec.get("title", "Section"))[:24],
                "rows": trimmed_rows,
            })

    payload = {
        "messaging_product": "whatsapp",
        "to": wa_id,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": body[:1024]},
            "action": {
                "button": "Choose",  # max 20 chars
                "sections": trimmed_sections,
            },
        },
    }
    resp = _post(payload)
    wa_msg_id = _extract_wa_msg_id(resp)

    _log_outbound(
        wa_id,
        text=body[:1024],
        msg_type="interactive",
        wa_msg_id=wa_msg_id,
        meta={"source": "bot", "kind": "list", "sections": len(trimmed_sections)},
    )
    return resp


def send_document(
    wa_id: str,
    url: str,
    filename: Optional[str] = None,
    caption: Optional[str] = None,
) -> dict:
    """Send a document (PDF, image, etc.) by public URL."""
    doc: dict = {"link": url}
    if filename:
        doc["filename"] = filename[:200]
    if caption:
        doc["caption"] = caption[:1024]

    payload = {
        "messaging_product": "whatsapp",
        "to": wa_id,
        "type": "document",
        "document": doc,
    }
    resp = _post(payload, timeout=30)
    wa_msg_id = _extract_wa_msg_id(resp)

    _log_outbound(
        wa_id,
        text=caption or "",
        msg_type="document",
        wa_msg_id=wa_msg_id,
        media_url=url,
        meta={"source": "bot", "filename": filename},
    )
    return resp


# Optional: add image/audio/video senders later if needed
# def send_image(...), send_template(...), etc.


# -------------------------------------------------------------------
# Health check
# -------------------------------------------------------------------
def ping() -> bool:
    """Quick sanity check for config."""
    return bool(WABA_TOKEN and PHONE_ID)


# Keep this so imports don't break
__all__ = [
    "send_text",
    "send_quick_replies",
    "send_list",
    "send_document",
    "ping",
]