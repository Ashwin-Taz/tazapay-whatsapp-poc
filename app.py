"""
Tazapay WhatsApp POC
--------------------
WhatsApp (Twilio) → Flask webhook → Claude AI + Tazapay tools → WhatsApp reply
"""

import os
import json
import hmac
import hashlib
import datetime
import logging
import requests
import urllib3

from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
import anthropic

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
ANTHROPIC_API_KEY    = os.getenv("ANTHROPIC_API_KEY", "")
TWILIO_ACCOUNT_SID   = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN    = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER", "whatsapp:+14155238886")
TAZAPAY_API_KEY      = os.getenv("TAZAPAY_API_KEY", "")
TAZAPAY_API_SECRET   = os.getenv("TAZAPAY_API_SECRET", "")
TAZAPAY_BASE_URL     = os.getenv("TAZAPAY_BASE_URL", "https://service-sandbox.tazapay.com")
AUTHORIZED_NUMBERS   = [n.strip() for n in os.getenv("AUTHORIZED_NUMBERS", "").split(",") if n.strip()]

# ---------------------------------------------------------------------------
# In-memory conversation store
# ---------------------------------------------------------------------------
conversation_store: dict = {}

# ---------------------------------------------------------------------------
# Tazapay API helpers — Basic Auth (API_KEY:API_SECRET base64 encoded)
# ---------------------------------------------------------------------------
def tazapay_get(path: str) -> dict:
    url = f"{TAZAPAY_BASE_URL}{path}"
    resp = requests.get(url, auth=(TAZAPAY_API_KEY, TAZAPAY_API_SECRET),
                        headers={"Content-Type": "application/json"},
                        timeout=15, verify=False)
    logger.info(f"Tazapay GET {path} → {resp.status_code}: {resp.text[:300]}")
    resp.raise_for_status()
    return resp.json()

def tazapay_post(path: str, payload: dict) -> dict:
    url = f"{TAZAPAY_BASE_URL}{path}"
    resp = requests.post(url, auth=(TAZAPAY_API_KEY, TAZAPAY_API_SECRET),
                         json=payload,
                         headers={"Content-Type": "application/json"},
                         timeout=15, verify=False)
    logger.info(f"Tazapay POST {path} → {resp.status_code}: {resp.text[:300]}")
    resp.raise_for_status()
    return resp.json()

# ---------------------------------------------------------------------------
# Tool definitions for Claude
# ---------------------------------------------------------------------------
TOOLS = [
    {
        "name": "check_balance",
        "description": "Fetch the merchant's Tazapay wallet balances. Optionally filter by currency (e.g. USD). Returns all balances if no currency specified.",
        "input_schema": {
            "type": "object",
            "properties": {
                "currency": {"type": "string", "description": "Optional 3-letter ISO currency code e.g. USD"}
            },
            "required": [],
        },
    },
    {
        "name": "get_fx_rate",
        "description": "Get the Tazapay FX rate between two currencies for a given amount.",
        "input_schema": {
            "type": "object",
            "properties": {
                "from_currency": {"type": "string"},
                "to_currency":   {"type": "string"},
                "amount":        {"type": "number"},
            },
            "required": ["from_currency", "to_currency", "amount"],
        },
    },
    {
        "name": "create_payment_link",
        "description": "Generate a Tazapay checkout payment link to collect funds from a customer.",
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_name":    {"type": "string"},
                "customer_email":   {"type": "string"},
                "customer_country": {"type": "string", "description": "ISO alpha-2 country code"},
                "amount":           {"type": "number"},
                "currency":         {"type": "string"},
                "description":      {"type": "string"},
            },
            "required": ["customer_name", "customer_email", "customer_country", "amount", "currency", "description"],
        },
    },
    {
        "name": "check_payout_status",
        "description": "Check the status of a Tazapay payout by its ID (starts with po_).",
        "input_schema": {
            "type": "object",
            "properties": {
                "payout_id": {"type": "string"},
            },
            "required": ["payout_id"],
        },
    },
]

SYSTEM_PROMPT = """You are a Tazapay payment assistant for merchants on WhatsApp.
You help with: checking balances, FX rates, creating payment links, and checking payout statuses.
Keep responses concise and WhatsApp-friendly (plain text, no markdown).
Always confirm key details before creating payment links.
Format numbers clearly with commas and 2 decimal places."""

# ---------------------------------------------------------------------------
# Tool executor
# ---------------------------------------------------------------------------
def execute_tool(name: str, inp: dict) -> str:
    logger.info(f"Tool: {name} | Input: {inp}")
    try:
        if name == "check_balance":
            currency = inp.get("currency", "").upper()
            data = tazapay_get("/v3/balance" + (f"?currency={currency}" if currency else ""))
            raw = data.get("data", data)
            if isinstance(raw, dict) and "balances" in raw:
                raw = raw["balances"]
            if isinstance(raw, dict):
                active = {k: v for k, v in raw.items() if float(v or 0) != 0}
                zero   = [k for k, v in raw.items() if float(v or 0) == 0]
                if active:
                    lines  = [f"{k}: {v}" for k, v in active.items()]
                    result = "Active balances:\n" + "\n".join(lines)
                    if zero:
                        result += f"\nZero: {', '.join(zero)}"
                else:
                    result = "All balances are zero."
            else:
                result = str(raw)
            return result

        elif name == "get_fx_rate":
            fc, tc, amt = inp["from_currency"].upper(), inp["to_currency"].upper(), int(inp["amount"])
            data = tazapay_get(f"/v3/fx?from={fc}&to={tc}&amount={amt}")
            raw  = data.get("data", data)
            rate      = raw.get("rate") if isinstance(raw, dict) else data.get("rate")
            converted = raw.get("converted_amount") if isinstance(raw, dict) else data.get("converted_amount")
            return f"FX Rate: 1 {fc} = {rate} {tc}\n{amt} {fc} = {converted} {tc}"

        elif name == "create_payment_link":
            payload = {
                "customer_details": {
                    "name": inp["customer_name"],
                    "email": inp["customer_email"],
                    "country": inp["customer_country"],
                },
                "invoice_currency": inp["currency"].upper(),
                "amount": inp["amount"],
                "transaction_description": inp["description"],
                "success_url": "https://tazapay.com/success",
                "cancel_url":  "https://tazapay.com/cancel",
            }
            data = tazapay_post("/v3/session/checkout", payload)
            raw  = data.get("data", data)
            url  = raw.get("url") or raw.get("payment_url") or raw.get("checkout_url", "N/A")
            sid  = raw.get("id", "N/A")
            return f"Payment link created!\nURL: {url}\nSession ID: {sid}"

        elif name == "check_payout_status":
            pid  = inp["payout_id"]
            data = tazapay_get(f"/v3/payout/{pid}")
            p    = data.get("data", data)
            return (
                f"Payout {pid}\n"
                f"Status: {p.get('status','unknown').upper()}\n"
                f"Amount: {p.get('amount','N/A')} {p.get('currency','')}\n"
                f"Beneficiary: {p.get('beneficiary_name', p.get('beneficiary',{}).get('name','N/A'))}\n"
                f"Created: {p.get('created_at','N/A')}"
            )
        else:
            return f"Unknown tool: {name}"

    except requests.HTTPError as e:
        logger.error(f"Tazapay HTTP error: {e.response.status_code} {e.response.text[:300]}")
        return f"Tazapay API error {e.response.status_code}: {e.response.text[:200]}"
    except Exception as e:
        logger.error(f"Tool error: {e}")
        return f"Error: {str(e)}"

# ---------------------------------------------------------------------------
# Claude agentic loop
# ---------------------------------------------------------------------------
def run_claude(phone: str, message: str) -> str:
    client  = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    history = conversation_store.get(phone, [])
    history.append({"role": "user", "content": message})

    for _ in range(5):
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=history,
        )
        history.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            text = " ".join(b.text for b in response.content if hasattr(b, "text"))
            conversation_store[phone] = history[-20:]
            return text.strip()

        elif response.stop_reason == "tool_use":
            results = []
            for b in response.content:
                if b.type == "tool_use":
                    result = execute_tool(b.name, b.input)
                    results.append({"type": "tool_result", "tool_use_id": b.id, "content": result})
            history.append({"role": "user", "content": results})
        else:
            break

    conversation_store[phone] = history[-20:]
    return "Sorry, I couldn't complete that request. Please try again."

# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------
@app.route("/webhook", methods=["POST"])
def webhook():
    from_number = request.form.get("From", "")
    body        = request.form.get("Body", "").strip()
    logger.info(f"Incoming message from {from_number}: {body}")

    if AUTHORIZED_NUMBERS and from_number not in AUTHORIZED_NUMBERS:
        resp = MessagingResponse()
        resp.message("Sorry, you are not authorized.")
        return str(resp)

    if not body or body.lower() in ["reset", "/reset"]:
        conversation_store.pop(from_number, None)
        resp = MessagingResponse()
        resp.message("Hi! I'm your Tazapay assistant. Ask me to:\n- Check balance\n- Get FX rate\n- Create payment link\n- Check payout status" if not body else "Conversation reset.")
        return str(resp)

    reply = run_claude(from_number, body)
    resp  = MessagingResponse()
    resp.message(reply)
    return str(resp)

@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok", "service": "tazapay-whatsapp-poc"}, 200

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    logger.info(f"Starting on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
