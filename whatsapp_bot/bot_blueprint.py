from __future__ import annotations

import os
import re
import json
import time
import traceback
from typing import Optional

from flask import Blueprint, request, abort
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

# Your backend API callers (these add X-Tenant-Id header etc.)
from whatsapp_bot.cart import add_to_cart, get_cart, clear_cart, update_cart, get_variants
from whatsapp_bot.orders import checkout, fetch_order, mpesa_stk

# AI + helpers
from whatsapp_bot.ai_router import llm_route
from whatsapp_bot.semantic_search import best_matches
from whatsapp_bot.memory import get_profile, update_last_order
# imports (ensure these exist)
from whatsapp_bot.catalog import fetch_menu, build_wa_sections, fetch_menu_pdf_urls
from whatsapp_bot.wa_api import send_text, send_quick_replies, send_list, send_document

bp = Blueprint("wa", __name__)
VERIFY_TOKEN = os.getenv("WABA_VERIFY_TOKEN", "change-me")
RESTAURANT_ID = int(os.getenv("RESTAURANT_ID", "1"))  # ← add this
_seen_inbound: set[str] = set()


WELCOME = (
    "🍔 *Welcome to QuickBite!*\n"
    "Just tell me what you’d like to eat or drink, and I’ll build your order.\n"
    "You can also say things like:\n"
    "• “show me the menu”\n"
    "• “what’s in my cart?”\n"
    "• “checkout”\n"
    "• “what’s the status of order 1234?”"
)

ITEM_RE = re.compile(r"^add_(\d+)$")

# -----------------------------------------------------------------------------
# Minimal in-memory deduper (process each WhatsApp message once)
# -----------------------------------------------------------------------------
_DEDUP_TTL = int(os.getenv("DEDUPE_TTL_SEC", "172800"))  # 48h
_seen: dict[str, float] = {}


def _claim_once(kind: str, wa_id: str, msg_id: Optional[str], payload: dict) -> bool:
    """
    Returns True only the first time a (kind, wa_id, msg_id) is seen during TTL.
    If msg_id is missing, hashes the payload (rare).
    """
    if msg_id:
        key = f"dedupe:{kind}:{wa_id}:{msg_id}"
    else:
        pkt = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        key = f"dedupe:{kind}:{wa_id}:{hash(pkt)}"

    now = time.time()
    # Best-effort sweep occasionally
    if _seen and len(_seen) % 100 == 0:
        for k, exp in list(_seen.items()):
            if exp < now:
                _seen.pop(k, None)

    if key in _seen:
        return False

    _seen[key] = now + _DEDUP_TTL
    return True


# -----------------------------------------------------------------------------
# Helpers for reading webhook payloads (defensive)
# -----------------------------------------------------------------------------
def _message(entry):
    """
    Safely extract the first message or None.
    Some webhooks (statuses, read receipts) don't include "messages".
    """
    changes = entry.get("changes") or []
    if not changes:
        return None
    value = changes[0].get("value") or {}
    msgs = value.get("messages") or []
    if not msgs:
        return None
    return msgs[0]


def _wa_id(entry) -> str:
    msg = _message(entry)
    if msg:
        return msg.get("from") or ""
    return ""


def _name(entry) -> str:
    changes = entry.get("changes") or []
    if not changes:
        return "Customer"
    value = changes[0].get("value") or {}
    contacts = value.get("contacts") or []
    if contacts:
        profile = contacts[0].get("profile") or {}
        return profile.get("name") or "Customer"
    return "Customer"


def _msg_id(entry) -> Optional[str]:
    msg = _message(entry)
    if msg:
        return msg.get("id")
    return None


_user_states: dict[str, dict] = {}


def _safe_int(val, default=None):
    try:
        if val is None:
            return default
        s = str(val).strip()
        if s in ("", "None", "null"):
            return default
        return int(s)
    except Exception:
        return default


def _parse_cmd(s: str):
    try:
        if not isinstance(s, str):
            return (False, None, None, None, None)
        parts = s.strip().split("|", 4)
        if len(parts) != 5:
            return (False, None, None, None, None)

        t, action, i, v, arg = parts
        if t != "CART":
            return (False, None, None, None, None)

        item_id = _safe_int(i)
        variant_id = _safe_int(v)
        arg_val = _safe_int(arg) if arg not in ("", "None", None) else arg
        return (True, action, item_id, variant_id, arg_val)
    except Exception:
        return (False, None, None, None, None)


# -----------------------------------------------------------------------------
# Verify (accept /webhook and /webhook/)
# -----------------------------------------------------------------------------
@bp.route("/webhook", methods=["GET"])
def verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        print("Webhook verified!")
        return challenge, 200
    abort(403)


# -----------------------------------------------------------------------------
# Note handling helper (for future item notes)
# -----------------------------------------------------------------------------
def _set_user_state(wa_id: str, state: dict):
    _user_states[wa_id] = state


def _get_user_state(wa_id: str) -> dict:
    return _user_states.get(wa_id, {})


def _clear_user_state(wa_id: str):
    _user_states.pop(wa_id, None)


def handle_note_message(wa_id: str, text: str) -> bool:
    """
    If the user is in 'await_note' mode, treat this text as a note for that cart item.
    """
    state = _get_user_state(wa_id)
    if state.get("mode") != "await_note":
        return False

    item_id = _safe_int(state.get("item_id"), default=None)
    variant_id = state.get("variant_id")
    if not item_id:
        _clear_user_state(wa_id)
        return False

    update_cart(
        wa_id,
        [
            {
                "op": "set_note",
                "item_id": item_id,
                "variant_id": variant_id,
                "note": text.strip()[:240],
            }
        ],
    )
    _clear_user_state(wa_id)
    _send_cart(wa_id, "📝 Note saved.\n")
    return True


# -----------------------------------------------------------------------------
# Inbound webhook (messages + interactives)
# -----------------------------------------------------------------------------
@bp.route("/webhook", methods=["POST"])
def inbound():
    data = request.get_json(force=True, silent=True) or {}
    wa_id = None
    customer_name = "Customer"

    try:
        print("[META HDR]", {k: v for k, v in request.headers.items() if k.lower().startswith("x-")}, flush=True)

        # === Find first real customer message (for logging later) ===
        first_customer_msg = None
        msg_wa_id = None
        for entry in data.get("entry", []):
            changes = entry.get("changes", [])
            if not changes:
                continue
            value = changes[0].get("value", {})
            messages = value.get("messages", [])
            if messages:
                first_customer_msg = messages[0]
                msg_wa_id = first_customer_msg.get("id")
                contacts = value.get("contacts", [])
                if contacts:
                    customer_name = contacts[0].get("profile", {}).get("name", "Customer")
                break  # we only need the first one

        # === Process ALL messages (in case of multiple in one webhook) ===
        for entry in data.get("entry", []):
            changes = entry.get("changes", [])
            if not changes:
                continue

            value = changes[0].get("value", {})
            messages = value.get("messages", [])
            if not messages:
                continue

            msg = messages[0]
            wa_id = msg.get("from")
            msg_id = msg.get("id")
            msg_type = msg.get("type")

            # Deduplicate processing (don't run logic twice)
            if not _claim_once("message", wa_id, msg_id, msg):
                print(f"[DEDUPED] {wa_id} | {msg_id}")
                continue

            preview = ""
            if msg_type == "text":
                preview = msg.get("text", {}).get("body", "")[:60]
            print(f"[INBOUND] {wa_id} | {msg_type} | {preview}")

            # Interactive messages
            if msg_type == "interactive":
                interactive = msg.get("interactive", {})
                itype = interactive.get("type")

                if itype == "button_reply":
                    button_id = interactive.get("button_reply", {}).get("id")
                    _handle_button(wa_id, customer_name, button_id)
                    continue

                if itype == "list_reply":
                    list_id = interactive.get("list_reply", {}).get("id")
                    _handle_list_selection(wa_id, customer_name, list_id)
                    continue

            # Text messages
            if msg_type == "text":
                raw_text = msg.get("text", {}).get("body", "").strip()

                if handle_note_message(wa_id, raw_text):
                    continue

                # IMPORTANT: pass original text (not forced lowercased) to AI
                _route_text(wa_id, customer_name, raw_text)
                continue

        # === SAVE INCOMING MESSAGE TO DATABASE — ONLY ONCE EVER (using wa_msg_id) ===
        if first_customer_msg and wa_id and msg_wa_id:
            try:
                # Global in-memory deduplication using Meta's official wa_msg_id
                if msg_wa_id in _seen_inbound:
                    print(f"[ADMIN LOG DEDUPED] wa_msg_id={msg_wa_id}")
                else:
                    _seen_inbound.add(msg_wa_id)
                    print(f"[ADMIN LOG OK] Saving new message wa_msg_id={msg_wa_id}")

                    backend_base = os.getenv("WHATSAPP_BACKEND_BASE", "http://localhost:8000").rstrip("/")
                    tenant_id = os.getenv("TENANT_ID", "1")

                    payload = {
                        "from": wa_id,
                        "display_name": customer_name,
                        "wa_msg_id": msg_wa_id,
                        "type": first_customer_msg.get("type", "text"),
                        "text": first_customer_msg.get("text", {}).get("body", "") if first_customer_msg.get("type") == "text" else "",
                        "timestamp": int(time.time()),
                        "meta": {"source": "customer"},
                    }

                    import requests

                    r = requests.post(
                        f"{backend_base}/v1/whatsapp/webhook_inbound",
                        json=payload,
                        headers={"X-Tenant-Id": tenant_id},
                        timeout=8,
                    )
                    if r.status_code != 200:
                        print(f"[ADMIN LOG FAILED] {r.status_code} {r.text}")
            except Exception as e:
                print("[ADMIN LOG CRASH]", e, flush=True)

        return "ok", 200

    except Exception as e:
        print("WEBHOOK CRASH:", e, flush=True)
        traceback.print_exc()
        if wa_id:
            try:
                send_text(wa_id, "Sorry, something went wrong. Please try again.")
            except Exception:
                pass
        return "ok", 200


# -----------------------------------------------------------------------------
# Routing / business logic
# -----------------------------------------------------------------------------
def _send_ai_reply(wa_id: str, parsed) -> None:
    """
    Send the LLM's natural-language reply if present.
    Safe even if llm_route doesn't return 'reply' yet.
    """
    msg = (getattr(parsed, "reply", "") or "").strip()
    if msg:
        send_text(wa_id, msg)


def _route_text(wa_id: str, name: str, text: str):
    text_lower = text.strip().lower()

    # === 1. Hard overrides (must work 100%) ===
    if any(x in text_lower for x in ["checkout", "pay", "place order", "i'm ready", "complete", "done", "yes"]):
        _do_checkout(wa_id, name, "pickup")
        return

    if text_lower.startswith("status"):
        code = text_lower.replace("status", "").strip()
        if code:
            try:
                o = fetch_order(code)
                send_text(wa_id, f"Order *{code}* is *{o['status'].upper()}* right now.")
            except:
                send_text(wa_id, "I can't find that order. Check the code and try again.")
        return

    if any(x in text_lower for x in ["cart", "my order", "what i have", "show me", "show cart"]):
        _send_cart(wa_id)
        return

    # === 2. Your beloved friendly Abdi prompt + RECOMMEND support ===
    try:
        cart = get_cart(wa_id)
        cart_text = ", ".join([f"{it['name']} ×{it['qty']}" for it in cart.get("items", [])]) or "nothing yet"
    except:
        cart_text = "nothing yet"

    prompt = f"""
You are Abdi, a super friendly waiter at QuickBite in Nairobi.
Customer name: {name.split()[0]}
Current cart: {cart_text}

Customer just said: "{text}"

Reply with exactly ONE of these formats ONLY:

ADD <item> ×<number>
REMOVE <item>
CHANGE <item> to ×<number>
CART
MENU
CHECKOUT
RECOMMEND anything
RECOMMEND spicy
RECOMMEND vegetarian
RECOMMEND under 800
RECOMMEND popular

Examples:
"another burger please" → ADD burger ×1
"remove the coke" → REMOVE coke
"make it three burgers" → CHANGE burger to ×3
"what's good?" → RECOMMEND popular
"something spicy under 1000" → RECOMMEND spicy under 1000
"veg options?" → RECOMMEND vegetarian

Current cart: {cart_text}
Customer: "{text}"
Reply:
"""

    try:
        response = llm_route(prompt, max_tokens=40, temperature=0.0).strip()
        print(f"[AI DECISION] {text} → {response}")
    except Exception as e:
        print("[LLM FAILED]", e)
        response = ""

    resp = response.upper()

    # === 3. Rock-solid action handling ===
    if resp.startswith("ADD "):
        item = response[4:].split("×")[0].strip().lower()
        qty = 1
        if "×" in response:
            try: qty = int(response.split("×")[1].strip())
            except: qty = 1

        menu = fetch_menu() or {}
        matches = [i for c in menu.get("categories", [])
                        for i in c.get("items", []) 
                        if item in i["name"].lower()]
        if matches:
            add_to_cart(wa_id, matches[0]["id"], qty)
            send_text(wa_id, f"Got it! Added {qty} × {matches[0]['name']}")
            _send_cart(wa_id, prefix="Your updated cart:\n")
        else:
            send_text(wa_id, f"Sorry, I don't have '{item}'. Say *menu* to see everything.")

    elif resp.startswith("REMOVE "):
        item = response[7:].strip().lower()
        cart_items = {i["name"].lower(): i for i in get_cart(wa_id).get("items", [])}
        match = next((v for k, v in cart_items.items() if item in k), None)
        if match:
            update_cart(wa_id, [{"op": "remove", "item_id": match["item_id"]}])
            send_text(wa_id, f"Removed {match['name']} from your cart")
            _send_cart(wa_id)

    elif resp.startswith("CHANGE "):
        parts = response[7:].split(" TO ×")
        if len(parts) == 2:
            item_name = parts[0].strip().lower()
            try:
                qty = int(parts[1].strip())
                cart_items = {i["name"].lower(): i for i in get_cart(wa_id).get("items", [])}
                match = next((v for k, v in cart_items.items() if item_name in k), None)
                if match:
                    update_cart(wa_id, [{"op": "set_qty", "item_id": match["item_id"], "qty": qty}])
                    send_text(wa_id, f"Updated to {qty} × {match['name']}")
                    _send_cart(wa_id)
            except: pass

    elif "CART" in resp:
        _send_cart(wa_id)

    elif "MENU" in resp:
        urls = _menu_pdf_urls()
        if urls:
            send_text(wa_id, "Here’s our full menu")
            send_document(wa_id, urls[0], caption="QuickBite Menu")
        else:
            send_text(wa_id, "Here’s what we have today")
            send_list(wa_id, "Menu", build_wa_sections(fetch_menu()))

    elif "CHECKOUT" in resp or "YES" in resp:
        _do_checkout(wa_id, name, "pickup")

    elif resp.startswith("RECOMMEND"):
        _send_recommendations(wa_id, text, response)

    else:
        send_text(wa_id, "Got it! Anything else you'd like?")
        time.sleep(0.3)
        _send_cart(wa_id)


# === Smart, friendly recommendations (feels like a real waiter) ===
def _send_recommendations(wa_id: str, user_text: str, ai_hint: str):
    menu = fetch_menu() or {}
    items = []
    for cat in menu.get("categories", []):
        for item in cat.get("items", []):
            tags = [t.lower() for t in item.get("tags", [])]
            price = int(item.get("price", 0))
            items.append({
                "name": item["name"],
                "price": price,
                "tags": tags,
                "popular": any(t in tags for t in ["popular", "best", "signature", "favourite"])
            })

    recs = items.copy()
    hint = ai_hint.lower()
    text_low = user_text.lower()

    # Smart filtering
    if any(w in text_low + hint for w in ["veg", "vegetarian", "no meat"]):
        recs = [i for i in recs if "vegetarian" in i["tags"] or "veg" in i["tags"]]
    if any(w in text_low + hint for w in ["spicy", "hot", "peri", "chilli"]):
        recs = [i for i in recs if "spicy" in i["tags"]]
    if any(w in text_low for w in ["under", "below", "max", "cheaper than"]):
        try:
            budget = int(''.join(filter(str.isdigit, text_low + hint)))
            recs = [i for i in recs if i["price"] <= budget]
        except: pass
    if any(w in text_low + hint for w in ["popular", "best", "good", "recommend", "your favorite", "signature"]):
        recs = [i for i in recs if i["popular"]] or sorted(recs, key=lambda x: x["price"])[:6]

    # Final sort: popular first, then price
    recs = sorted(recs, key=lambda x: (-x["popular"], x["price"]))[:6]

    if not recs:
        send_text(wa_id, "Hmm, nothing matches right now. Try saying *menu*")
        return

    lines = ["Here are my top picks for you:"]
    for r in recs:
        lines.append(f"• {r['name']} — KSh {r['price']}")
    lines.append("\nJust say the name to add it")

    send_text(wa_id, "\n".join(lines))


# -----------------------------------------------------------------------------
# Variant + edit postback handlers
# -----------------------------------------------------------------------------
def _cmd(action: str, item_id: int, variant_id: int | None = None, arg: int | str = 0) -> str:
    safe_item = item_id if item_id is not None else 0
    safe_variant = variant_id if variant_id is not None else 0
    return f"CART|{action}|{safe_item}|{safe_variant}|{arg}"


def handle_variant_choose(wa_id: str, cmd_id: str) -> bool:
    """
    Handle CART|VAR_CHOOSE|<item_id>|<old_variant_id>|<new_variant_id>
    """
    is_cart, action, item_id, old_variant_id, new_variant_id = _parse_cmd(cmd_id)
    if not (is_cart and action == "VAR_CHOOSE"):
        return False

    update_cart(
        wa_id,
        [
            {
                "op": "change_variant",
                "item_id": item_id,
                "old_variant_id": old_variant_id,
                "new_variant_id": int(new_variant_id),
            }
        ],
    )
    _send_cart(wa_id, "🔁 Variant updated.\n")
    return True


# -----------------------------------------------------------------------------
# Buttons / list selections
# -----------------------------------------------------------------------------
def _handle_button(wa_id: str, name: str, btn_id: str):
    """
    Routes button presses by id.

    - CART|... → edit/variant commands
    - menu/browse_menu → show menu list
    - download_menu → send PDF
    - view_cart / checkout / edit cart / pickup / delivery → classic flows
    """
    if not btn_id:
        send_text(wa_id, "Okay!")
        return ("ok", 200)

    # First: CART postbacks (edit quantity/variant/note)
    if btn_id.startswith("CART|"):
        if handle_variant_choose(wa_id, btn_id) or handle_edit_postback(wa_id, btn_id):
            return ("ok", 200)

    bid = btn_id.lower()

    if bid in ("menu", "browse_menu"):
        # Safe to send a List now (user tapped a button)
        menu = fetch_menu() or {}
        sections = build_wa_sections(menu)
        sections = (sections or [])
        if sections:
            send_list(wa_id, "Browse our menu 👇", sections)
        else:
            send_text(wa_id, "Menu unavailable.")
        return ("ok", 200)

    if bid == "download_menu":
        urls = _menu_pdf_urls()
        print("[MENU PDF URLS]", urls, flush=True)

        if urls:
            send_document(wa_id, urls[0], filename="Menu.pdf", caption="Menu")
        else:
            send_text(wa_id, "No menu PDF found.")
        return ("ok", 200)

    if bid == "view_cart":
        _send_cart(wa_id)
        return ("ok", 200)

    if bid == "checkout":
        # Still use quick replies here, but you could let AI infer from text instead.
        send_quick_replies(
            wa_id,
            "Pickup or Delivery?",
            ["Pickup", "Delivery", "View Cart"],
        )
        return ("ok", 200)

    if bid == "edit cart":
        handle_edit_cart(wa_id)
        return ("ok", 200)

    if bid == "pickup":
        _do_checkout(wa_id, name, "pickup")
        return ("ok", 200)

    if bid == "delivery":
        send_text(
            wa_id,
            "Please reply with your delivery address (one line).",
        )
        send_text(
            wa_id,
            "e.g. *Westlands, The Oval, 6th floor* — then send *checkout* again.",
        )
        return ("ok", 200)

    send_text(wa_id, "Okay!")
    return ("ok", 200)


def _handle_list_selection(wa_id: str, name: str, sel_id: str):
    if (sel_id or "").startswith("CART|EDIT_PICK|"):
        if handle_edit_pick(wa_id, sel_id):
            return ("ok", 200)


# -----------------------------------------------------------------------------
# Action helpers (buttons following list/results)
# -----------------------------------------------------------------------------
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")


def _force_public_base(url: str) -> str:
    if not PUBLIC_BASE_URL:
        return url
    u = urlparse(url)
    pb = urlparse(PUBLIC_BASE_URL)
    # keep path/query/fragment; replace scheme+netloc
    return urlunparse((pb.scheme, pb.netloc, u.path, u.params, u.query, u.fragment))


def _ensure_restaurant_id(url: str, rid: int | None) -> str:
    # if caller passed a restaurant_id, enforce it; otherwise, preserve existing or default to 1
    target_rid = rid if rid is not None else 1
    u = urlparse(url)
    q = dict(parse_qsl(u.query, keep_blank_values=True))
    if "restaurant_id" not in q:
        q["restaurant_id"] = str(target_rid)
        u = u._replace(query=urlencode(q))
    return urlunparse(u)


def _menu_pdf_urls(restaurant_id: int | None = None) -> list[str]:
    try:
        urls = fetch_menu_pdf_urls(
            restaurant_id=restaurant_id
        )  # calls /v1/public/menu_pdf
        urls = [u for u in urls if isinstance(u, str) and u.startswith("http")]
        # 1) force public host, 2) ensure ?restaurant_id=
        urls = [
            _ensure_restaurant_id(_force_public_base(u), restaurant_id) for u in urls
        ]
        print("[MENU PDF URLS FINAL]", urls, flush=True)
        return urls
    except Exception as e:
        print("[_menu_pdf_urls ERROR]", repr(e), flush=True)
        return []


def _send_menu_actions(wa_id: str):
    # Kept for backward-compat; used only in fallbacks now.
    urls = _menu_pdf_urls()
    if urls:
        send_quick_replies(
            wa_id,
            "What would you like to do?",
            ["View Cart", "Checkout", "Download Menu"],
        )
    else:
        send_quick_replies(
            wa_id,
            "What would you like to do?",
            ["View Cart", "Checkout", "Menu"],
        )


def _send_menu_actions_after_list(wa_id: str, body: str):
    """
    Legacy helper – currently not used in AI-first flow,
    but kept for compatibility.
    """
    urls = _menu_pdf_urls()
    if urls:
        send_quick_replies(
            wa_id,
            body,
            ["Add first", "Menu", "Download Menu"],
        )
    else:
        send_quick_replies(
            wa_id,
            body,
            ["Add first", "Menu", "View Cart"],
        )


def _send_default_actions(wa_id: str):
    """
    Very last-resort fallback, when AI fails completely.
    """
    urls = _menu_pdf_urls()
    if urls:
        send_quick_replies(
            wa_id,
            "What would you like to do?",
            ["Menu", "View Cart", "Download Menu"],
        )
    else:
        send_quick_replies(
            wa_id,
            "What would you like to do?",
            ["Menu", "View Cart", "Checkout"],
        )


def _send_menu_entrypoint(wa_id: str):
    # Only show Download if a PDF exists
    urls = _menu_pdf_urls()
    if urls:
        send_quick_replies(
            wa_id,
            "How would you like to view the menu?",
            ["Browse Menu", "Download Menu"],
        )
    else:
        send_quick_replies(
            wa_id,
            "How would you like to view the menu?",
            ["Browse Menu"],
        )


# -----------------------------------------------------------------------------
# Edit-cart UI
# -----------------------------------------------------------------------------
def handle_edit_cart(wa_id: str):
    """
    1) Send a text summary of the cart
    2) Send a single list message to select which item to edit
    """
    c = get_cart(wa_id)
    items = c.get("items", [])
    if not items:
        send_text(wa_id, "🧺 Your cart is empty. Tell me what you’d like to order.")
        return

    # 1) Summary text
    lines = ["🛒 *Your cart:*"]
    total = 0.0
    for it in items:
        name = it.get("name", "Item")
        qty = int(it.get("qty", 1))
        price = float(it.get("price", 0))
        total += qty * price
        lines.append(
            f"• {name} ×{qty} — KSh {int(qty*price) if float(qty*price).is_integer() else qty*price}"
        )
    lines.append(
        f"\nSubtotal: *KSh {int(total) if float(total).is_integer() else total}*"
    )
    lines.append("\n✏️ Select an item to edit:")
    send_text(wa_id, "\n".join(lines))

    # 2) One list message (WhatsApp: 1 interactive per message; list allows many options)
    #    WA limit: 10 rows per section, 10 sections max → chunk items into groups of 10
    sections = []
    chunk = []
    for idx, it in enumerate(items, start=1):
        raw_item_id = it.get("item_id") or it.get("id")
        item_id = _safe_int(raw_item_id, default=0)
        variant_id = it.get("variant_id") or 0
        name = it.get("name", f"Item {item_id}")
        qty = int(it.get("qty", 1))
        row_id = _cmd("EDIT_PICK", item_id, variant_id, 0)  # will parse later
        row_title = f"{name}"[:24]  # UI-safe
        row_desc = f"Qty {qty}"[:72]
        chunk.append({"id": row_id, "title": row_title, "description": row_desc})
        if len(chunk) == 10:
            sections.append({"title": "Cart Items", "rows": chunk})
            chunk = []
    if chunk:
        sections.append({"title": "Cart Items", "rows": chunk})

    send_list(wa_id, "Select an item to edit:", sections)


def handle_edit_pick(wa_id: str, sel_id: str):
    """
    sel_id is encoded as: CART|EDIT_PICK|<item_id>|<variant_id>|<line_id_or_idx>
    e.g. built via: _cmd("EDIT_PICK", item_id, variant_id or 0, line_id or idx)
    """
    is_cart, action, i, v, arg = _parse_cmd(sel_id)
    if not (is_cart and action == "EDIT_PICK"):
        return False

    item_id = _safe_int(i, default=None)
    variant_id = _safe_int(v, default=None)

    if not item_id:
        send_text(wa_id, "Sorry, I couldn’t identify that cart item. Try again.")
        return True

    # Per-item controls; keep titles ≤20 chars
    send_quick_replies(
        wa_id,
        "Adjust quantity:",
        [
            {"id": _cmd("DEC", item_id, variant_id or 0, -1), "title": "−1"},
            {"id": _cmd("INC", item_id, variant_id or 0, +1), "title": "+1"},
            {"id": _cmd("RM", item_id, variant_id or 0, 0), "title": "Remove"},
        ],
    )
    time.sleep(0.2)
    send_quick_replies(
        wa_id,
        "Other actions:",
        [
            {"id": _cmd("VAR", item_id, variant_id or 0, 0), "title": "Change Variant"},
            {"id": _cmd("NOTE", item_id, variant_id or 0, 0), "title": "Add Note"},
            {"id": _cmd("BACK", 0, 0, 0), "title": "Back to Cart"},
        ],
    )
    return True


def _extras_section_if_any() -> list[dict]:
    """
    Returns a single-section list for the WA List message when a PDF is available.
    Example:
      [{"title": "Extras", "rows":[{"id":"download_menu","title":"Download Menu (PDF)"}]}]
    or [] if no PDF.
    """
    urls = _menu_pdf_urls()
    if not urls:
        return []
    return [
        {
            "title": "Extras",
            "rows": [
                {
                    "id": "download_menu",
                    "title": "Download Menu (PDF)",
                    "description": "Get the full menu as a PDF",
                }
            ],
        }
    ]


# -----------------------------------------------------------------------------
# Variant picker
# -----------------------------------------------------------------------------
def _prompt_variant_picker(wa_id: str, item_id: int, current_variant_id: int | None):
    # If you have a backend endpoint, use get_variants(item_id); otherwise fallback
    variants = []
    if callable(get_variants):
        try:
            variants = get_variants(item_id) or []
        except Exception:
            variants = []

    if not variants:
        send_text(wa_id, "No other variants available for this item.")
        return

    send_text(wa_id, "Choose a variant:")
    group = []
    for v in variants:
        vid = int(v.get("id") or v.get("variant_id"))
        title = v.get("name") or f"Variant {vid}"
        # We use the button *id* to carry the command; title is the user-facing label
        group.append({"id": _cmd("VAR_CHOOSE", item_id, current_variant_id or 0, vid), "title": title})
        if len(group) == 3:
            send_quick_replies(wa_id, "Variants:", group)
            group = []
    if group:
        send_quick_replies(wa_id, "Variants:", group)


# -----------------------------------------------------------------------------
# Cart & checkout helpers
# -----------------------------------------------------------------------------
def _send_cart(wa_id: str, prefix: str = ""):
    try:
        c = get_cart(wa_id)
        items = c.get("items", [])
        if not items:
            send_text(wa_id, "Cart empty")
            return
        total = sum(int(i["qty"]) * float(i["price"]) for i in items)
        lines = ["Cart:"]
        for i in items: lines.append(f"• {i['name']}×{i['qty']}")
        lines.append(f"Total KSh {int(total)}")
        send_text(wa_id, "\n".join(lines))
    except:
        send_text(wa_id, "Cart error")

def _do_checkout(wa_id: str, name: str, method: str):
    try:
        o = checkout(wa_id, name, wa_id, method=method, address=None)
        code = o.get("code") or o.get("id")
        msg = f"🎉 Order placed! Code: *{code}*\nWe’ll confirm shortly."
        # Optional M-Pesa STK:
        # try:
        #     mpesa_stk(o["id"], wa_id)
        # except Exception as pay_e:
        #     print("[MPESA WARN]", pay_e, flush=True)
        send_text(wa_id, msg)
        try:
            update_last_order(wa_id, o.get("items", []))
        except Exception:
            pass
    except Exception as e:
        print("[CHECKOUT ERROR]", e, flush=True)
        traceback.print_exc()
        send_text(wa_id, "Checkout failed. Please try again or send *help*.")
