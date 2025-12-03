
import os
import json
from typing import Literal, List, Optional, Dict, Any
from pydantic import BaseModel, Field
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# ---------- Tool schema the model must populate ----------
class LineItem(BaseModel):
    item_id: Optional[str] = None        # fill if known
    item_name: Optional[str] = None      # raw name from user or matched item title
    qty: int = 1
    options: Dict[str, Any] = Field(default_factory=dict)  # e.g. {"no_onions": True, "cheese": "extra"}

class ParsedOrder(BaseModel):
    action: Literal[
        "ADD_TO_CART","SHOW_MENU","ASK_QUESTION","CHECKOUT",
        "ORDER_STATUS","VIEW_CART","CLEAR_CART","SMALL_TALK",
        "EDIT_SET_QTY","EDIT_REMOVE","EDIT_CHANGE_VARIANT","EDIT_SET_NOTE"
    ]
    items: List[LineItem] = Field(default_factory=list)
    target_item_name: Optional[str] = None
    target_item_id: Optional[str] = None
    target_variant_name: Optional[str] = None
    target_variant_id: Optional[str] = None
    new_qty: Optional[int] = None
    new_variant_name: Optional[str] = None
    new_variant_id: Optional[str] = None
    note_text: Optional[str] = None
    budget_kes: Optional[float] = None
    dietary: List[str] = Field(default_factory=list)
    spice_level: Optional[str] = None
    fulfillment: Optional[Literal["pickup","delivery"]] = None
    delivery_address: Optional[str] = None
    order_code: Optional[str] = None
    clarifications: List[str] = Field(default_factory=list)
    reasoning_notes: Optional[str] = None
    response_text: Optional[str] = None  # NEW: For natural, human-like responses

SYSTEM = """You are a restaurant ordering AI for WhatsApp, designed to respond conversationally like a friendly human. 
- Parse messy, multilingual messages (English/Swahili slang) into a JSON object matching the schema.
- Default qty=1 if not stated. Infer simple options (no onions, extra cheese, well done).
- Respect constraints like budget/spice/dietary from user_profile or message.
- If the request lacks a required slot (e.g., delivery address for CHECKOUT), set clarifications.
- For ORDER_STATUS, extract order_code if present.
- For ambiguous items, keep item_name and do NOT invent item_id.
- For conversational cart edits (e.g., "make the burger 3", "remove juice", "switch burger to large", "add note less spicy"):
  - Ground references in CART_SNAPSHOT. Prefer exact item_id/variant_id from snapshot.
  - If ambiguous (multiple matches), add one short clarification in 'clarifications' and STOP.
  - Coerce quantities to integer >= 0 (qty=0 means remove).
  - For variant changes, set new_variant_id if you can match by name; otherwise ask clarification.
- Use MENU_SNAPSHOT for structured item data and MENU_TEXT for detailed menu descriptions (e.g., for ASK_QUESTION or SHOW_MENU).
- Generate a natural, human-like response in response_text (e.g., "Added that burger for you! Want to see your cart or keep browsing?").
- Suggest next steps casually in response_text (e.g., "What next—edit your cart, checkout, or add more?").
- Only output JSON via the tool. Never free text.
"""

def llm_route(
    user_text: str,
    menu_snapshot: str,
    user_profile: str,
    cart_snapshot: str,
    menu_text: str = ""  # NEW: Added menu_text parameter
) -> ParsedOrder:
    """
    Parse user input and generate a natural response using the LLM.
    menu_snapshot: JSON string of lean menu [{id,name,price,tags,options?}]
    user_profile: JSON string of known prefs {"last_order":[...], "dietary":[...], "allergies":[...]}
    cart_snapshot: JSON string of current cart lines [{item_id,name,variant_id,variant,qty,price}]
    menu_text: Extracted text from menu PDF for detailed answers
    """
    tools = [{
        "type": "function",
        "function": {
            "name": "emit_parsed_order",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": [
                        "ADD_TO_CART", "SHOW_MENU", "ASK_QUESTION", "CHECKOUT",
                        "ORDER_STATUS", "VIEW_CART", "CLEAR_CART", "SMALL_TALK",
                        "EDIT_SET_QTY", "EDIT_REMOVE", "EDIT_CHANGE_VARIANT", "EDIT_SET_NOTE"
                    ]},
                    "items": {"type": "array", "items": {
                        "type": "object", "properties": {
                            "item_id": {"type": ["string", "null"]},
                            "item_name": {"type": ["string", "null"]},
                            "qty": {"type": "integer"},
                            "options": {"type": "object"}
                        }, "required": ["qty", "options"]
                    }},
                    "target_item_name": {"type": ["string", "null"]},
                    "target_item_id": {"type": ["string", "null"]},
                    "target_variant_name": {"type": ["string", "null"]},
                    "target_variant_id": {"type": ["string", "null"]},
                    "new_qty": {"type": ["integer", "null"]},
                    "new_variant_name": {"type": ["string", "null"]},
                    "new_variant_id": {"type": ["string", "null"]},
                    "note_text": {"type": ["string", "null"]},
                    "budget_kes": {"type": ["number", "null"]},
                    "dietary": {"type": "array", "items": {"type": "string"}},
                    "spice_level": {"type": ["string", "null"]},
                    "fulfillment": {"type": ["string", "null"], "enum": ["pickup", "delivery"]},
                    "delivery_address": {"type": ["string", "null"]},
                    "order_code": {"type": ["string", "null"]},
                    "clarifications": {"type": "array", "items": {"type": "string"}},
                    "reasoning_notes": {"type": ["string", "null"]},
                    "response_text": {"type": ["string", "null"]}  # NEW: For natural responses
                },
                "required": ["action", "items", "clarifications", "response_text"]
            }
        }
    }]

    # Construct prompt with menu_text
    prompt = (
        f"MENU_SNAPSHOT:\n{menu_snapshot}\n\n"
        f"MENU_TEXT:\n{menu_text[:10000]}\n\n"  # Truncate to avoid context limits
        f"PROFILE:\n{user_profile}\n\n"
        f"CART_SNAPSHOT:\n{cart_snapshot}\n\n"
        f"USER:\n{user_text}"
    )
    msg = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": prompt},
    ]

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=msg,
            tools=tools,
            tool_choice={"type": "function", "function": {"name": "emit_parsed_order"}},
            temperature=0.2
        )
        args = resp.choices[0].message.tool_calls[0].function.arguments
        data = json.loads(args)
        return ParsedOrder(**data)
    except Exception as e:
        print("[LLM ROUTE ERROR]", e, flush=True)
        return ParsedOrder(
            action="SMALL_TALK",
            items=[],
            clarifications=[],
            response_text="Sorry, I didn't catch that. Could you try again or ask for the menu?"
        )
